from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    OWNER = "owner"
    MANAGER = "manager"
    REVIEWER = "reviewer"
    CONTRIBUTOR = "contributor"
    SUPPORT = "support"


PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.OWNER: frozenset({"*"}),
    Role.MANAGER: frozenset({"projects.read", "projects.manage", "projects.review", "team.invite", "reports.read", "messages.reply", "customers.read"}),
    Role.REVIEWER: frozenset({"projects.read", "projects.review", "messages.reply"}),
    Role.CONTRIBUTOR: frozenset({"projects.assigned.read", "projects.execute", "messages.reply"}),
    Role.SUPPORT: frozenset({"messages.reply", "customers.read"}),
}


def has_permission(role: str | Role, permission: str) -> bool:
    try:
        actual = role if isinstance(role, Role) else Role(role)
    except ValueError:
        return False
    grants = PERMISSIONS[actual]
    return "*" in grants or permission in grants


def role_label(role: str) -> str:
    return {
        "owner": "مالک اصلی",
        "manager": "مدیر پروژه",
        "reviewer": "بازبین",
        "contributor": "همکار اجرایی",
        "support": "پشتیبانی",
    }.get(role, "نامشخص")
