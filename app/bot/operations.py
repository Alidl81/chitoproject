from datetime import UTC, timedelta
from math import ceil

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AdminUser,
    Conversation,
    ConversationMessage,
    OutboxMessage,
    PlatformIdentity,
    Project,
    ProjectDelivery,
    ProjectEvent,
    now_utc,
)
from app.security import random_token

TERMINAL = {"completed", "declined", "canceled"}


def remaining_label(project: Project) -> str:
    if not project.work_due_at or project.status in TERMINAL:
        return ""
    due = project.work_due_at
    if due.tzinfo is None:
        due = due.replace(tzinfo=UTC)
    seconds = (due - now_utc()).total_seconds()
    return f" · {ceil(seconds / 86400)} روز مانده" if seconds > 0 else " · مهلت گذشته"


async def member_chat(db: AsyncSession, member: AdminUser, preferred: str):
    identities = list(await db.scalars(select(PlatformIdentity).where(
        PlatformIdentity.admin_id == member.id, PlatformIdentity.platform.in_(("telegram", "bale")),
    ).order_by((PlatformIdentity.platform == preferred).desc(), PlatformIdentity.id)))
    if not identities:
        return None
    identity = identities[0]
    chat = await db.scalar(select(Conversation).where(
        Conversation.platform == identity.platform, Conversation.chat_id == identity.chat_id,
    ))
    if not chat:
        chat = Conversation(platform=identity.platform, chat_id=identity.chat_id, admin_id=member.id)
        db.add(chat)
        await db.flush()
    return chat if chat.admin_id == member.id else None


async def enqueue_text(db: AsyncSession, chat: Conversation, body: str, key: str, *,
                       keyboard=None, label: str = "تیم چیتوپروژه", due=None):
    message = ConversationMessage(conversation_id=chat.id, direction="outbound",
                                  actor_label=label, content=body, metadata_json={"delivery_status": "queued"})
    db.add(message)
    await db.flush()
    db.add(OutboxMessage(
        dedupe_key=key, platform=chat.platform, chat_id=chat.chat_id, conversation_id=chat.id,
        transcript_message_id=message.id, operation="send", body=body, keyboard=keyboard,
        next_attempt_at=due or now_utc(),
    ))
    return message


async def cancel_reminders(db: AsyncSession, project_id: str):
    await db.execute(update(OutboxMessage).where(
        OutboxMessage.dedupe_key.startswith(f"deadline:{project_id}:"),
        OutboxMessage.status.in_(("pending", "processing")),
    ).values(status="canceled", lease_until=None))


async def schedule_reminders(db: AsyncSession, project: Project, member: AdminUser, preferred: str):
    await cancel_reminders(db, project.id)
    if not project.work_due_at or not (chat := await member_chat(db, member, preferred)):
        return
    now = now_utc()
    deadline = project.work_due_at if project.work_due_at.tzinfo else project.work_due_at.replace(tzinfo=UTC)
    for days in (2, 1):
        due = deadline - timedelta(days=days)
        if due > now:
            await enqueue_text(
                db, chat, f"یادآوری: {days} روز تا مهلت «{project.title}» باقی مانده.",
                f"deadline:{project.id}:{days}:{random_token(8)}",
                keyboard=[[{"text": "باز کردن پروژه", "callback_data": f"a:project:{project.public_id}"}]], due=due,
            )


async def approve_delivery(db: AsyncSession, actor: AdminUser, delivery_id: str) -> bool:
    delivery = await db.get(ProjectDelivery, delivery_id)
    if not delivery:
        return False
    project = await db.get(Project, delivery.project_id, with_for_update=True)
    await db.refresh(delivery)
    if (not project or project.archived_at or project.status != "in_progress" or delivery.status != "pending"
            or not delivery.submitted_by or project.assigned_admin_id != delivery.submitted_by):
        return False
    customer = await db.get(Conversation, project.conversation_id)
    if not customer or customer.customer_id != project.customer_id or customer.admin_id is not None:
        return False
    delivery.status = "approved"
    project.status = "completed"
    project.updated_at = now_utc()
    await cancel_reminders(db, project.id)
    db.add(ProjectEvent(project_id=project.id, actor_type="admin", actor_id=str(actor.id),
                        event_type="delivery_approved", note="فایل‌های تحویل تأیید شدند.", private=False))
    await enqueue_text(
        db, customer, f"پروژهٔ «{project.title}» تکمیل شد. برای دریافت فایل‌ها دکمهٔ زیر را بزن.",
        f"delivery-approved:{delivery.id}",
        keyboard=[[{"text": "دریافت فایل‌های پروژه", "callback_data": f"c:files:{delivery.id}", "style": "success"}]],
    )
    return True
