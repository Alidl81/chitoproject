from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.models import AdminInvite, AdminUser, AuditEvent, LoginThrottle, OwnerActivation
from app.permissions import Role
from app.security import digest_token, hash_password, now_utc, verify_password
from app.web.common import (
    COOKIE_NAME,
    FORM_CSRF_COOKIE,
    create_session,
    new_form_csrf,
    redirect,
    render,
    set_form_csrf_cookie,
    set_session_cookie,
    valid_double_submit,
)

router = APIRouter(prefix="/admin", tags=["admin-auth"])
_DUMMY_HASH = hash_password("not-a-real-password-for-timing")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _ip_key(request: Request, scope: str = "login") -> str:
    address = request.client.host if request.client else "unknown"
    return hashlib.sha256(f"{scope}:{address}".encode("utf-8", errors="replace")).hexdigest()


async def _locked(db: AsyncSession, key: str) -> bool:
    row = await db.get(LoginThrottle, key)
    if not row or not row.locked_until:
        return False
    return _aware(row.locked_until) > now_utc()


async def _record_failure(db: AsyncSession, key: str) -> None:
    now = now_utc()
    row = await db.scalar(select(LoginThrottle).where(LoginThrottle.key == key).with_for_update())
    if row is None:
        row = LoginThrottle(key=key, failures=1, window_started_at=now)
        db.add(row)
    else:
        if now - _aware(row.window_started_at) > timedelta(minutes=settings.login_lock_minutes):
            row.failures = 0
            row.window_started_at = now
            row.locked_until = None
        row.failures += 1
    if row.failures >= settings.login_max_attempts:
        row.locked_until = now + timedelta(minutes=settings.login_lock_minutes)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        row = await db.scalar(select(LoginThrottle).where(LoginThrottle.key == key).with_for_update())
        if row is None:
            return
        row.failures += 1
        if row.failures >= settings.login_max_attempts:
            row.locked_until = now + timedelta(minutes=settings.login_lock_minutes)
        await db.commit()


async def _clear_failures(db: AsyncSession, key: str) -> None:
    row = await db.get(LoginThrottle, key)
    if row:
        await db.delete(row)
        await db.commit()


def _set_public_form_cookie(request: Request, response: RedirectResponse, token: str) -> None:
    set_form_csrf_cookie(response, token)


@router.get("/login")
async def login_page(request: Request, db: AsyncSession = Depends(get_session)):
    token = new_form_csrf(request)
    response = render(request, "login.html", notice=request.query_params.get("notice", ""), csrf_token=token)
    set_form_csrf_cookie(response, token)
    return response


@router.post("/login")
async def login(
    request: Request,
    db: AsyncSession = Depends(get_session),
    username: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
):
    key = _ip_key(request)
    if not valid_double_submit(request, csrf_token):
        return redirect("/admin/login", "csrf")
    if await _locked(db, key):
        return redirect("/admin/login", "locked")
    admin = await db.scalar(select(AdminUser).where(func.lower(AdminUser.username) == username.strip().lower()))
    password_ok = await asyncio.to_thread(verify_password, password, admin.password_hash if admin else _DUMMY_HASH)
    if not admin or not admin.active or not password_ok:
        await _record_failure(db, key)
        return redirect("/admin/login", "invalid")
    await _clear_failures(db, key)
    admin.last_login_at = now_utc()
    raw, row = await create_session(db, admin)
    db.add(AuditEvent(actor_admin_id=admin.id, action="auth.login", object_type="admin", object_id=str(admin.id), detail={}))
    await db.commit()
    response = RedirectResponse("/admin", status_code=303)
    set_session_cookie(response, raw)
    response.delete_cookie(FORM_CSRF_COOKIE, path="/admin", secure=settings.is_production)
    return response


@router.post("/logout")
async def logout(
    request: Request,
    db: AsyncSession = Depends(get_session),
    csrf_token: str = Form(""),
):
    raw = request.cookies.get(COOKIE_NAME, "")
    if raw:
        from app.models import AdminSession

        auth_row = await db.get(AdminSession, digest_token(raw))
        if auth_row and hmac.compare_digest(auth_row.csrf_token, csrf_token):
            db.add(AuditEvent(actor_admin_id=auth_row.admin_id, action="auth.logout", object_type="admin", object_id=str(auth_row.admin_id), detail={}))
            await db.delete(auth_row)
            await db.commit()
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/admin", secure=settings.is_production, httponly=True, samesite="lax")
    return response


@router.get("/activate")
async def activate_page(request: Request, db: AsyncSession = Depends(get_session)):
    owner_count = await db.scalar(select(func.count(AdminUser.id)).where(AdminUser.role == Role.OWNER.value)) or 0
    token = new_form_csrf(request)
    response = render(request, "activate.html", csrf_token=token,
                      available=owner_count == 0, notice=request.query_params.get("notice", ""))
    set_form_csrf_cookie(response, token)
    return response


@router.post("/activate")
async def activate_owner(
    request: Request,
    db: AsyncSession = Depends(get_session),
    activation_code: str = Form(""),
    username: str = Form(""),
    display_name: str = Form(""),
    password: str = Form(""),
    password_confirm: str = Form(""),
    csrf_token: str = Form(""),
):
    if not valid_double_submit(request, csrf_token):
        return redirect("/admin/activate", "csrf")
    throttle_key = _ip_key(request, "activate")
    if await _locked(db, throttle_key):
        return redirect("/admin/activate", "locked")
    normalized = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{3,80}", normalized) or password != password_confirm:
        return redirect("/admin/activate", "fields")
    activation = await db.scalar(select(OwnerActivation).where(OwnerActivation.id == 1).with_for_update())
    owner_count = await db.scalar(select(func.count(AdminUser.id)).where(AdminUser.role == Role.OWNER.value)) or 0
    if not activation or activation.used_at or _aware(activation.expires_at) <= now_utc() or owner_count:
        return redirect("/admin/activate", "unavailable")
    if not activation_code or not hmac.compare_digest(digest_token(activation_code.strip()), activation.token_digest):
        await _record_failure(db, throttle_key)
        return redirect("/admin/activate", "code-invalid")
    try:
        password_hash = await asyncio.to_thread(hash_password, password)
    except ValueError:
        return redirect("/admin/activate", "password")
    owner = AdminUser(username=normalized, display_name=display_name.strip()[:160], password_hash=password_hash,
                      role=Role.OWNER.value, active=True)
    db.add(owner)
    activation.used_at = now_utc()
    try:
        await db.flush()
        db.add(AuditEvent(actor_admin_id=owner.id, action="owner.activated", object_type="admin", object_id=str(owner.id), detail={}))
        raw, auth_row = await create_session(db, owner)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return redirect("/admin/activate", "username")
    response = RedirectResponse("/admin", status_code=303)
    await _clear_failures(db, throttle_key)
    set_session_cookie(response, raw)
    response.delete_cookie(FORM_CSRF_COOKIE, path="/admin", secure=settings.is_production)
    return response


@router.get("/join")
async def join_page(request: Request, db: AsyncSession = Depends(get_session)):
    csrf = new_form_csrf(request)
    response = render(request, "join.html", invite=None, available=True, csrf_token=csrf,
                      notice=request.query_params.get("notice", ""))
    set_form_csrf_cookie(response, csrf)
    return response


@router.post("/join")
async def accept_invite(
    request: Request,
    db: AsyncSession = Depends(get_session),
    invite_token: str = Form(""),
    username: str = Form(""),
    display_name: str = Form(""),
    password: str = Form(""),
    password_confirm: str = Form(""),
    csrf_token: str = Form(""),
):
    if not valid_double_submit(request, csrf_token):
        return redirect("/admin/join", "csrf")
    throttle_key = _ip_key(request, "join")
    if await _locked(db, throttle_key):
        return redirect("/admin/join", "locked")
    token = invite_token.strip()
    if len(token) > 128 or not token:
        await _record_failure(db, throttle_key)
        return redirect("/admin/join", "invite-unavailable")
    invite = await db.scalar(select(AdminInvite).where(AdminInvite.token_digest == digest_token(token)).with_for_update())
    if not invite or invite.used_at or _aware(invite.expires_at) <= now_utc():
        await _record_failure(db, throttle_key)
        return redirect("/admin/login", "invite-unavailable")
    normalized = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{3,80}", normalized) or password != password_confirm:
        return redirect("/admin/join", "fields")
    try:
        password_hash = await asyncio.to_thread(hash_password, password)
    except ValueError:
        return redirect("/admin/join", "password")
    member = AdminUser(username=normalized, display_name=display_name.strip()[:160], password_hash=password_hash,
                       role=invite.role, active=True)
    db.add(member)
    invite.used_at = now_utc()
    try:
        await db.flush()
        invite.used_by = member.id
        db.add(AuditEvent(action="team.invite_accepted", object_type="admin", object_id=str(member.id), detail={"role": member.role}))
        raw, auth_row = await create_session(db, member)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return redirect("/admin/join", "username")
    response = RedirectResponse("/admin", status_code=303)
    await _clear_failures(db, throttle_key)
    set_session_cookie(response, raw)
    response.delete_cookie(FORM_CSRF_COOKIE, path="/admin", secure=settings.is_production)
    return response
