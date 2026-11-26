from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, ConversationMessage, Customer, Project, now_utc

TEHRAN = ZoneInfo("Asia/Tehran")


def tehran_today() -> date:
    return now_utc().astimezone(TEHRAN).date()


async def bot_stats(db: AsyncSession, days: int, through: date | None = None) -> tuple[list[dict], int]:
    days = max(1, min(days, 90))
    today = through or tehran_today()
    first_day = today - timedelta(days=days - 1)
    start = datetime.combine(first_day, time.min, tzinfo=TEHRAN).astimezone(UTC)
    end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=TEHRAN).astimezone(UTC)
    buckets = {
        first_day + timedelta(days=i): {"starts": set(), "submitters": set(), "new_users": set()}
        for i in range(days)
    }

    def day(value: datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=UTC)
        return aware.astimezone(TEHRAN).date()

    queries = {
        "starts": select(Conversation.customer_id, ConversationMessage.created_at)
        .join(Conversation, Conversation.id == ConversationMessage.conversation_id)
        .where(Conversation.customer_id.is_not(None), ConversationMessage.direction == "inbound",
               ConversationMessage.revision == 1, ConversationMessage.created_at >= start,
               ConversationMessage.created_at < end,
               or_(ConversationMessage.content == "/start",
                   ConversationMessage.content.startswith("/start "),
                   ConversationMessage.content.startswith("/start@"))),
        "submitters": select(Project.customer_id, Project.created_at)
        .where(Project.created_at >= start, Project.created_at < end),
        "new_users": select(Customer.id, Customer.created_at)
        .where(Customer.created_at >= start, Customer.created_at < end),
    }
    for metric, query in queries.items():
        async for customer_id, created_at in await db.stream(query):
            if (bucket := buckets.get(day(created_at))) is not None:
                bucket[metric].add(customer_id)
    return (
        [{"day": d.isoformat(), **{k: len(ids) for k, ids in row.items()}} for d, row in buckets.items()],
        int(await db.scalar(select(func.count(Customer.id))) or 0),
    )
