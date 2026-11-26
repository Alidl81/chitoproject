from datetime import timedelta
from urllib.parse import urlsplit

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app import checkout
from app.bot.flow import _stage_action
from app.bot.payment import archive_password, stage_job, stage_payload
from app.config import settings
from app.database import get_session
from app.main import app
from app.models import (
    Conversation,
    Customer,
    OutboxMessage,
    PaymentStage,
    Project,
    ProjectDelivery,
    now_utc,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("recover_without_callback", [False, True])
async def test_website_checkout_unlocks_only_bound_paid_order(db_context, monkeypatch, recover_without_callback):
    db, maker = db_context
    origin = "https://shop.example.test"
    authority = "A" + "7" * 35
    monkeypatch.setattr(settings, "checkout_base_url", origin)
    monkeypatch.setattr(settings, "payment_archive_key", SecretStr("archive test key".ljust(40, "!")))
    customer = Customer(display_name="مینا")
    db.add(customer)
    await db.flush()
    chat = Conversation(platform="telegram", chat_id="42", customer_id=customer.id, state="home", draft_data={})
    db.add(chat)
    await db.flush()
    project = Project(public_id="CP-WEB", title="تست وب", description="شرح", status="in_progress",
                      customer_id=customer.id, conversation_id=chat.id, staged_payment=True)
    db.add(project)
    await db.flush()
    delivery = ProjectDelivery(project_id=project.id, status="stage_reviewed")
    db.add(delivery)
    await db.flush()
    stage = PaymentStage(project_id=project.id, delivery_id=delivery.id, number=1, stage_count=1, title="تحویل نهایی",
                         amount_toman=70000, preview_key="a" * 32, archive_key="b" * 32, website_checkout=True)
    db.add(stage)
    await db.flush()
    await stage_job(db, chat, stage, "stage_video")
    payload = stage_payload(stage)
    video = await db.scalar(select(OutboxMessage).where(OutboxMessage.dedupe_key == f"stage_video:{payload}"))
    video.status = "sent"
    await _stage_action(db, chat, "pa", stage.id, stage.version, {"update_id": 1})
    assert stage.status == "awaiting_payment"
    assert timedelta(hours=5, minutes=59) < stage.deadline - now_utc() <= timedelta(hours=6)
    notice = await db.scalar(select(OutboxMessage).where(OutboxMessage.dedupe_key.startswith("ui:telegram:1:")))
    order_url = notice.keyboard[0][0]["url"]
    assert order_url.startswith(f"{origin}/orders#")
    assert not (await db.scalars(select(OutboxMessage).where(OutboxMessage.operation == "stage_card"))).all()
    token = urlsplit(order_url).fragment
    order = f"/orders/{stage.id}"
    download = order + "/download"
    archive = b"stored encrypted zip"
    (settings.uploads_dir / stage.archive_key).write_bytes(archive)
    await db.commit()

    gateway_calls = []
    provider_verified = False
    provider_status = "IN_BANK"

    async def provider(action, data):
        gateway_calls.append((action, data))
        if action == "request":
            return {"code": 100, "authority": authority}
        if action == "inquiry":
            return {"code": 100, "status": provider_status}
        return {"code": 101, "ref_id": 32123} if provider_verified else {"code": -50}

    monkeypatch.setattr(checkout, "_provider", provider)

    async def override_session():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        transport = httpx.ASGITransport(app=app)
        async with (httpx.AsyncClient(transport=transport, base_url=origin) as client,
                    httpx.AsyncClient(transport=transport, base_url=origin) as stranger):
            assert (await stranger.get(download)).status_code == 404
            assert (await client.post("/orders/access", data={"token": token})).status_code == 403
            headers = {"Origin": origin}
            wrong = token[:-1] + ("0" if token[-1] != "0" else "1")
            assert (await client.post("/orders/access", data={"token": wrong}, headers=headers)).status_code == 200
            access = await client.post("/orders/access", data={"token": token}, headers=headers)
            assert access.headers["location"] == order
            assert "HttpOnly" in access.headers["set-cookie"]
            assert "Secure" in access.headers["set-cookie"]
            assert (await client.get(download)).status_code == 404
            assert "درگاه پرداخت فعلاً در دسترس نیست" in (await client.get(order)).text
            assert (await client.post(order + "/pay", headers=headers)).headers["location"] == order
            assert not gateway_calls

            monkeypatch.setattr(settings, "zarinpal_merchant_id", SecretStr("0" * 36))
            for _ in range(2):
                result = await client.post(order + "/pay", headers=headers)
                assert result.headers["location"] == checkout.pay_url(authority)
            assert len(gateway_calls) == 1
            request = gateway_calls[0][1]
            assert request["amount"] == 700000
            assert request["callback_url"] == origin + "/orders/return"

            status_url = f"/orders/return?Authority={authority}&Status="
            assert (await stranger.get(status_url + "NOK")).status_code == 303
            assert len(gateway_calls) == 1
            assert (await client.post(order + "/verify", headers=headers)).headers["location"].endswith("unverified")
            assert gateway_calls[-1][0] == "inquiry"
            assert (await stranger.get(status_url + "OK")).headers["location"].endswith("unverified")
            assert (await client.get(download)).status_code == 404
            assert archive_password(stage.id, stage.version) not in (await client.get(order)).text

            provider_verified = True
            provider_status = "PAID"
            response = (await client.post(order + "/verify", headers=headers) if recover_without_callback
                        else await client.get(status_url + "OK"))
            assert response.headers["location"].endswith("=verified")
            assert gateway_calls[-1] == ("verify", {"amount": 700000, "authority": authority})
            length = len(gateway_calls)
            assert (await stranger.get(status_url + "OK")).headers["location"].endswith("verified")
            assert len(gateway_calls) == length
            assert (await stranger.get(download)).status_code == 404
            result_page = (await client.get(order)).text
            assert "32123" in result_page and archive_password(stage.id, stage.version) in result_page
            response = await client.get(download)
            assert response.status_code == 200 and response.content == archive
            assert "attachment" in response.headers["content-disposition"]
    finally:
        app.dependency_overrides.pop(get_session, None)

    await db.refresh(project)
    await db.refresh(stage)
    assert project.status == "completed"
    assert stage.status == "paid"
    assert not (await db.scalars(select(OutboxMessage).where(
        OutboxMessage.operation.in_({"stage_card", "stage_zip", "stage_secret"}),
    ))).all()
