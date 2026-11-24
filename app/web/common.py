from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AdminSession, AdminUser
from app.permissions import has_permission, role_label
from app.security import digest_token, now_utc, random_token

COOKIE_NAME = "chitoproject_session"
FORM_CSRF_COOKIE = "ph_form_csrf"
templates = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"),
    autoescape=select_autoescape(["html", "xml"]),
)


def format_local(value: datetime) -> str:
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return aware.astimezone(ZoneInfo("Asia/Tehran")).strftime("%Y-%m-%d · %H:%M")


def notice_label(value: str) -> str:
    labels = {
        "csrf": "فرم منقضی شده است؛ صفحه را تازه کنید.",
        "locked": "تلاش‌های ورود زیاد بوده است. کمی بعد دوباره امتحان کنید.",
        "invalid": "نام کاربری یا رمز عبور معتبر نیست.",
        "fields": "اطلاعات واردشده کامل یا معتبر نیست.",
        "password": "رمز عبور باید دست‌کم ۱۴ نویسه داشته باشد.",
        "unavailable": "فعال‌سازی در دسترس نیست یا قبلاً مصرف شده است.",
        "code-invalid": "کد فعال‌سازی معتبر نیست.",
        "username": "این نام کاربری قبلاً استفاده شده است.",
        "invite-unavailable": "این دعوت منقضی شده یا قبلاً استفاده شده است.",
        "role-not-allowed": "انتخاب این نقش برای حساب شما مجاز نیست.",
        "invalid-assignee": "همکار انتخاب‌شده در دسترس نیست.",
        "empty-reply": "متن پاسخ نمی‌تواند خالی باشد.",
        "conversation-missing": "گفتگوی مربوط به این درخواست پیدا نشد.",
        "invalid-transition": "تغییر وضعیت انتخاب‌شده برای این مرحله مجاز نیست.",
        "status-saved": "وضعیت پروژه ذخیره و برای مشتری در صف ارسال قرار گرفت.",
        "assigned": "پروژه به همکار انتخاب‌شده واگذار شد.",
        "reply-queued": "پاسخ در صف ارسال قرار گرفت.",
        "note-saved": "یادداشت داخلی ذخیره شد.",
        "missing": "گفتگوی موردنظر پیدا نشد.",
        "transcript-deleted": "آرشیو گفتگو حذف شد؛ رویداد حذف در گزارش باقی ماند.",
        "bot-link-unavailable": "لینک پیام‌رسان در دسترس نیست. نام کاربری ربات را بررسی کنید.",
        "cannot-deactivate": "این عضو را نمی‌توان غیرفعال کرد.",
        "member-deactivated": "دسترسی همکار لغو شد و نشست‌های او باطل شدند.",
        "password-mismatch": "رمز فعلی یا تکرار رمز جدید با هم سازگار نیست.",
        "password-policy": "رمز جدید باید دست‌کم ۱۴ نویسه داشته باشد.",
        "password-updated": "رمز تغییر کرد؛ دوباره وارد شوید.",
        "sign-in": "برای ادامه وارد حساب تیمی خود شوید.",
    }
    return labels.get(value, "درخواست انجام نشد یا صفحه به‌روز شده است.")


templates.globals.update(role_label=role_label, format_local=format_local, notice_label=notice_label)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def current_admin(request: Request, db: AsyncSession) -> AdminUser | None:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    row = await db.scalar(select(AdminSession).where(AdminSession.token_digest == digest_token(raw)))
    if not row or _aware(row.expires_at) <= now_utc():
        return None
    admin = await db.get(AdminUser, row.admin_id)
    if not admin or not admin.active or admin.session_version != row.session_version:
        await db.delete(row)
        await db.commit()
        return None
    return admin


async def current_session(request: Request, db: AsyncSession) -> AdminSession | None:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    row = await db.scalar(select(AdminSession).where(AdminSession.token_digest == digest_token(raw)))
    if not row or _aware(row.expires_at) <= now_utc():
        return None
    admin = await db.get(AdminUser, row.admin_id)
    if not admin or not admin.active or admin.session_version != row.session_version:
        await db.delete(row)
        await db.commit()
        return None
    return row


def render(request: Request, template: str, *, admin: AdminUser | None = None, status_code: int = 200,
           **context: Any) -> HTMLResponse:
    page = templates.get_template(template)
    data: dict[str, Any] = {
        "request": request,
        "admin": admin,
        "csrf_token": "",
        "can_projects": bool(admin and (has_permission(admin.role, "projects.read") or has_permission(admin.role, "projects.assigned.read"))),
        "can_team": bool(admin and has_permission(admin.role, "team.invite")),
        "can_reports": bool(admin and has_permission(admin.role, "reports.read")),
        "has_customers": bool(admin and has_permission(admin.role, "customers.read")),
        "current_path": request.url.path,
    }
    data.update(context)
    return HTMLResponse(page.render(**data), status_code=status_code)


def new_form_csrf(request: Request) -> str:
    existing = request.cookies.get(FORM_CSRF_COOKIE)
    if existing and "." in existing:
        token, signature = existing.split(".", 1)
        expected = digest_token(f"csrf:{token}")
        if token and hmac.compare_digest(signature, expected):
            return token
    return random_token(24)


def set_form_csrf_cookie(response: RedirectResponse | HTMLResponse, token: str) -> None:
    signature = digest_token("csrf:" + token)
    signed = f"{token}.{signature}"
    response.set_cookie(FORM_CSRF_COOKIE, signed, max_age=900, httponly=True,
                        secure=settings.is_production, samesite="lax", path="/admin")


def valid_double_submit(request: Request, submitted: str) -> bool:
    cookie = request.cookies.get(FORM_CSRF_COOKIE, "")
    if not cookie or "." not in cookie or not submitted:
        return False
    token, signature = cookie.split(".", 1)
    expected = digest_token(f"csrf:{token}")
    return bool(hmac.compare_digest(token, submitted) and hmac.compare_digest(signature, expected))


def valid_session_csrf(row: AdminSession, submitted: str) -> bool:
    return bool(submitted and hmac.compare_digest(row.csrf_token, submitted))


async def create_session(db: AsyncSession, admin: AdminUser) -> tuple[str, AdminSession]:
    now = now_utc()
    await db.execute(delete(AdminSession).where(
        (AdminSession.expires_at <= now)
        | ((AdminSession.admin_id == admin.id) & (AdminSession.session_version != admin.session_version))
    ))
    raw = random_token(32)
    row = AdminSession(
        token_digest=digest_token(raw), admin_id=admin.id, session_version=admin.session_version,
        csrf_token=random_token(24), expires_at=now + timedelta(hours=settings.session_ttl_hours),
    )
    db.add(row)
    return raw, row


def set_session_cookie(response: RedirectResponse | HTMLResponse, raw: str) -> None:
    response.set_cookie(COOKIE_NAME, raw, max_age=settings.session_ttl_hours * 3600,
                        httponly=True, secure=settings.is_production, samesite="lax", path="/admin")


def clear_session_cookie(response: RedirectResponse) -> None:
    response.delete_cookie(COOKIE_NAME, path="/admin", secure=settings.is_production,
                           httponly=True, samesite="lax")


def redirect(path: str, notice: str | None = None) -> RedirectResponse:
    target = path if not notice else f"{path}?notice={notice}"
    return RedirectResponse(target, status_code=303)
