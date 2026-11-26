from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import checkout
from app.bot.operations import cancel_reminders
from app.bot.payment import archive_password, asset_path, is_expired
from app.database import get_session
from app.models import PaymentStage, Project, ProjectEvent, now_utc
from app.web.common import format_local, templates

router = APIRouter(prefix="/orders")
_COOKIE = "chito_order"


def _page(request: Request, **values) -> HTMLResponse:
    return HTMLResponse(
        templates.get_template("checkout.html").render(request=request, **values),
        headers={"Referrer-Policy": "origin", "Cache-Control": "private, no-store"},
    )


def _check_origin(request: Request) -> None:
    if not checkout.website_enabled() or request.headers.get("origin") != checkout.checkout_url():
        raise HTTPException(403)


async def _order(db: AsyncSession, stage_id: str, *, lock: bool = False):
    query = select(PaymentStage, Project).join(Project, Project.id == PaymentStage.project_id).where(
        PaymentStage.id == stage_id, PaymentStage.website_checkout.is_(True),
    )
    if lock:
        query = query.with_for_update(of=PaymentStage)
    return (await db.execute(query)).one_or_none()


async def _authorized_order(request: Request, db: AsyncSession, stage_id: str, *, lock: bool = False):
    scope = checkout.access_scope(request.cookies.get(_COOKIE, ""))
    if not scope or scope[0] != stage_id:
        return None
    pair = await _order(db, stage_id, lock=lock)
    if not pair or pair[0].version != scope[1] or pair[0].status not in {"awaiting_payment", "paid"}:
        return None
    return pair


def _payable(stage: PaymentStage, project: Project) -> bool:
    return (stage.status == "awaiting_payment" and project.status == "in_progress"
            and not project.archived_at and not is_expired(stage) and bool(checkout.merchant()))


@router.get("")
async def checkout_entry(request: Request):
    return _page(request, entry=True)


@router.post("/access")
async def checkout_access(request: Request, token: str = Form(""), db: AsyncSession = Depends(get_session)):
    _check_origin(request)
    scope = checkout.access_scope(token)
    if not scope:
        return _page(request)
    stage_id, version = scope
    pair = await _order(db, stage_id)
    if not pair or pair[0].version != version or pair[0].status not in {"awaiting_payment", "paid"}:
        return _page(request)
    response = RedirectResponse(f"/orders/{stage_id}", status_code=303)
    response.set_cookie(
        _COOKIE, token, max_age=int(checkout.ACCESS_LIFETIME.total_seconds()), httponly=True,
        secure=True, samesite="lax", path=f"/orders/{stage_id}",
    )
    return response


async def _record_payment(db: AsyncSession, stage: PaymentStage) -> bool:
    if stage.status == "paid":
        return True
    try:
        reference = await checkout.verify_payment(stage)
    except checkout.GatewayError:
        return False
    project = await db.get(Project, stage.project_id, with_for_update=True)
    stage.gateway_reference, stage.verified_at, stage.status = reference, now_utc(), "paid"
    db.add(ProjectEvent(project_id=project.id, actor_type="system", event_type="stage_payment_verified",
                        note=f"پرداخت مرحلهٔ {stage.number} در زرین‌پال تأیید شد.", private=True))
    if stage.number == stage.stage_count and project.status == "in_progress":
        project.status = "completed"
        await cancel_reminders(db, project.id)
    await db.commit()
    return True


@router.get("/return")
async def gateway_return(request: Request, Authority: str = "", Status: str = "", db: AsyncSession = Depends(get_session)):
    if not checkout.valid_authority(Authority):
        return _page(request)
    stage = await db.scalar(select(PaymentStage).where(
        PaymentStage.gateway_authority == Authority, PaymentStage.website_checkout.is_(True),
    ).with_for_update())
    if not stage:
        return _page(request)
    result = "unverified"
    if stage.status == "paid" or (Status == "OK" and await _record_payment(db, stage)):
        result = "verified"
    return RedirectResponse(f"/orders/{stage.id}?result={result}", status_code=303)


@router.get("/{stage_id}")
async def checkout_order(stage_id: str, request: Request, db: AsyncSession = Depends(get_session)):
    pair = await _authorized_order(request, db, stage_id)
    if not pair:
        return _page(request)
    stage, project = pair
    paid = stage.status == "paid" and bool(stage.verified_at and stage.gateway_reference)
    return _page(
        request, stage=stage, project=project, paid=paid, payable=_payable(stage, project),
        merchant_ready=bool(checkout.merchant()), expired=is_expired(stage), result=request.query_params.get("result"),
        deadline=format_local(stage.deadline) if stage.deadline else "",
        password=archive_password(stage.id, stage.version) if paid else "",
    )


@router.post("/{stage_id}/pay")
async def checkout_pay(stage_id: str, request: Request, db: AsyncSession = Depends(get_session)):
    _check_origin(request)
    pair = await _authorized_order(request, db, stage_id, lock=True)
    if not pair or not _payable(*pair):
        return RedirectResponse(f"/orders/{stage_id}", status_code=303)
    stage, project = pair
    if not stage.gateway_authority:
        try:
            stage.gateway_authority = await checkout.open_payment(stage, project.public_id)
        except checkout.GatewayError:
            return RedirectResponse(f"/orders/{stage.id}?result=unavailable", status_code=303)
        await db.commit()
    return RedirectResponse(checkout.pay_url(stage.gateway_authority), status_code=303,
                            headers={"Referrer-Policy": "origin"})


@router.post("/{stage_id}/verify")
async def checkout_retry(stage_id: str, request: Request, db: AsyncSession = Depends(get_session)):
    _check_origin(request)
    pair = await _authorized_order(request, db, stage_id, lock=True)
    if not pair:
        raise HTTPException(404)
    stage, _ = pair
    verified = stage.status == "paid"
    if not verified and stage.gateway_authority:
        try:
            if await checkout.payment_was_charged(stage):
                verified = await _record_payment(db, stage)
        except checkout.GatewayError:
            pass
    result = "verified" if verified else "unverified"
    return RedirectResponse(f"/orders/{stage_id}?result={result}", status_code=303)


@router.get("/{stage_id}/download")
async def checkout_download(stage_id: str, request: Request, db: AsyncSession = Depends(get_session)):
    pair = await _authorized_order(request, db, stage_id)
    if not pair:
        raise HTTPException(404)
    stage, _ = pair
    if stage.status != "paid" or not stage.verified_at or not stage.gateway_reference:
        raise HTTPException(404)
    try:
        path = asset_path(stage.archive_key)
    except ValueError as exc:
        raise HTTPException(404) from exc
    return FileResponse(path, filename=f"chitoproject-stage-{stage.number}.zip",
                        media_type="application/zip", headers={"Cache-Control": "private, no-store"})
