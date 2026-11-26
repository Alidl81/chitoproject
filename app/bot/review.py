from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, ConversationMessage, OutboxMessage


async def dismiss_previews(db: AsyncSession, review_key: str) -> None:
    notices = list(await db.scalars(select(OutboxMessage).where(
        OutboxMessage.dedupe_key.startswith(f"{review_key}:"),
        OutboxMessage.operation == "send",
    ).with_for_update()))
    for notice in notices:
        if notice.status == "pending":
            notice.status = "canceled"
            continue
        if notice.status not in {"sent", "processing"} or not notice.transcript_message_id:
            continue
        key = f"dismiss-review:{notice.id}"
        if await db.scalar(select(OutboxMessage.id).where(OutboxMessage.dedupe_key == key)):
            continue
        db.add(OutboxMessage(
            dedupe_key=key, platform=notice.platform, chat_id=notice.chat_id,
            conversation_id=notice.conversation_id, operation="review_del",
            transcript_message_id=notice.transcript_message_id, body="",
        ))


def record_decision(db: AsyncSession, admin_id: int, kind: str, object_id: str,
                    accepted: bool, reason: str) -> None:
    db.add(AuditEvent(
        actor_admin_id=admin_id, action=f"{kind}.review", object_type=kind, object_id=object_id,
        detail={"decision": "accepted" if accepted else "rejected", "reason": reason},
    ))


def support_context(message: ConversationMessage) -> bool:
    return bool(
        message.direction == "inbound"
        and (message.metadata_json or {}).get("support_session_id")
        and not message.content.startswith("/")
    )
