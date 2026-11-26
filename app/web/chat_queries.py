from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased

from app.models import (
    Attachment,
    Conversation,
    ConversationFile,
    ConversationMessage,
    ConversationRead,
)


def important_incoming():
    message = ConversationMessage
    support = and_(
        message.direction == "inbound",
        message.metadata_json["support_session_id"].as_string().is_not(None),
        ~message.content.startswith("/"),
    )
    submitted = and_(
        message.direction == "event",
        or_(message.message_kind == "project_submitted", message.content.like("درخواست CP-% ثبت شد.")),
    )
    return or_(support, submitted)


def latest_revision():
    newer = aliased(ConversationMessage)
    return ~select(newer.id).where(
        newer.conversation_id == ConversationMessage.conversation_id,
        newer.provider_message_id == ConversationMessage.provider_message_id,
        newer.revision > ConversationMessage.revision,
    ).correlate(ConversationMessage).exists()


def important_history():
    message = ConversationMessage
    project_file = select(ConversationFile.id).join(Attachment, Attachment.stored_key == ConversationFile.stored_key).where(
        ConversationFile.message_id == message.id,
    ).correlate(message).exists()
    return and_(
        latest_revision(),
        or_(important_incoming(), project_file,
            and_(message.direction == "outbound", message.actor_label != "پیام راهنمای تیم",
                 message.metadata_json["broadcast_id"].as_string().is_(None))),
    )


def unread_messages(admin_id: int, *, conversation_id=None):
    customer_chat = aliased(Conversation)
    filters = [
        customer_chat.customer_id.is_not(None), customer_chat.admin_id.is_(None),
        important_incoming(), latest_revision(),
        or_(ConversationRead.read_at.is_(None), ConversationMessage.created_at > ConversationRead.read_at),
    ]
    if conversation_id is not None:
        filters.append(ConversationMessage.conversation_id == conversation_id)
    return (
        select(func.count(ConversationMessage.id)).select_from(ConversationMessage)
        .join(customer_chat, customer_chat.id == ConversationMessage.conversation_id)
        .outerjoin(ConversationRead, and_(ConversationRead.admin_id == admin_id,
                                         ConversationRead.conversation_id == ConversationMessage.conversation_id))
        .where(*filters)
    )
