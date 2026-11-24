"""Frozen initial schema for a fresh installation.

Revision ID: 0001_initial
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "admin_users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(80), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(24), nullable=False),
        sa.Column("display_name", sa.String(160), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("session_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "platform_identities",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("provider_user_id", sa.String(100), nullable=False),
        sa.Column("chat_id", sa.String(100), nullable=False),
        sa.Column("username", sa.String(100), nullable=True),
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("admin_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("platform", "provider_user_id", name="uq_identity_platform_user"),
        sa.UniqueConstraint("platform", "admin_id", name="uq_identity_platform_admin"),
    )
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("chat_id", sa.String(100), nullable=False),
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("admin_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("draft_data", sa.JSON(), nullable=False),
        sa.Column("ui_message_id", sa.String(100), nullable=True),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("platform", "chat_id", name="uq_conversation_platform_chat"),
    )
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider_message_id", sa.String(100), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(12), nullable=False),
        sa.Column("actor_label", sa.String(160), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("message_kind", sa.String(30), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("deleted_from_chat", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "provider_message_id", "revision", name="uq_message_revision"),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("public_id", sa.String(24), nullable=False),
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("deadline_note", sa.String(120), nullable=False),
        sa.Column("budget_toman", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("assigned_admin_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "project_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(80), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("private", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "conversation_files",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("message_id", sa.String(36), sa.ForeignKey("conversation_messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stored_key", sa.String(64), nullable=False, unique=True),
        sa.Column("original_name", sa.String(200), nullable=False),
        sa.Column("mime_type", sa.String(120), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "attachments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stored_key", sa.String(64), nullable=False, unique=True),
        sa.Column("original_name", sa.String(200), nullable=False),
        sa.Column("mime_type", sa.String(120), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "admin_invites",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("role", sa.String(24), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_table(
        "owner_activation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "bot_link_challenges",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("admin_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "admin_sessions",
        sa.Column("token_digest", sa.String(64), primary_key=True),
        sa.Column("admin_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_version", sa.Integer(), nullable=False),
        sa.Column("csrf_token", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "login_throttles",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "incoming_updates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("update_id", sa.String(100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(120), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("platform", "update_id", name="uq_incoming_platform_update"),
    )
    op.create_table(
        "poller_leases",
        sa.Column("platform", sa.String(16), primary_key=True),
        sa.Column("owner", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("dedupe_key", sa.String(160), nullable=False, unique=True),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("chat_id", sa.String(100), nullable=False),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("transcript_message_id", sa.String(36), sa.ForeignKey("conversation_messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("operation", sa.String(12), nullable=False),
        sa.Column("provider_message_id", sa.String(100), nullable=True),
        sa.Column("use_as_ui", sa.Boolean(), nullable=False),
        sa.Column("force_send", sa.Boolean(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("keyboard", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("actor_admin_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("object_type", sa.String(40), nullable=False),
        sa.Column("object_id", sa.String(100), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    for table, column in (
        ("platform_identities", "platform"), ("platform_identities", "customer_id"),
        ("platform_identities", "admin_id"),
        ("conversations", "platform"), ("conversations", "customer_id"), ("conversations", "admin_id"),
        ("conversation_messages", "conversation_id"),
        ("projects", "customer_id"), ("projects", "conversation_id"),
        ("projects", "status"), ("projects", "assigned_admin_id"),
        ("project_events", "project_id"),
        ("conversation_files", "conversation_id"), ("conversation_files", "message_id"),
        ("conversation_files", "sha256"),
        ("attachments", "project_id"), ("attachments", "sha256"),
        ("bot_link_challenges", "admin_id"),
        ("admin_sessions", "admin_id"), ("admin_sessions", "expires_at"),
        ("outbox_messages", "conversation_id"),
        ("audit_events", "actor_admin_id"), ("audit_events", "action"),
    ):
        op.create_index(f"ix_{table}_{column}", table, [column])
    for table, column in (
        ("admin_users", "username"),
        ("projects", "public_id"),
        ("admin_invites", "token_digest"),
        ("bot_link_challenges", "token_digest"),
    ):
        op.create_index(f"ix_{table}_{column}", table, [column], unique=True)
    op.create_index("ix_projects_status_updated", "projects", ["status", "updated_at"])
    op.create_index("ix_message_conversation_created", "conversation_messages", ["conversation_id", "created_at"])
    op.create_index("ix_incoming_ready", "incoming_updates", ["status", "received_at"])
    op.create_index("ix_outbox_ready", "outbox_messages", ["status", "next_attempt_at"])


def downgrade() -> None:
    for table in (
        "audit_events", "outbox_messages", "poller_leases", "incoming_updates", "login_throttles",
        "admin_sessions", "bot_link_challenges", "owner_activation", "admin_invites", "attachments",
        "conversation_files", "project_events", "projects", "conversation_messages", "conversations",
        "platform_identities", "admin_users", "customers",
    ):
        op.drop_table(table)
