from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AuthenticatedIdentity:
    session_id: str
    employee_code: str
    employee_name: str = ""
    role_id: str = ""
    role_name: str = ""
    department: str = ""
    position: str = ""
    email: str = ""
    mobile: str = ""
    user_id: str = ""
    permissions: set[str] = field(default_factory=set)
    raw_employee: dict[str, Any] = field(default_factory=dict)


_current_identity: ContextVar[AuthenticatedIdentity | None] = ContextVar("current_identity", default=None)
_current_bearer_token: ContextVar[str | None] = ContextVar("current_bearer_token", default=None)
_current_database_target: ContextVar[str | None] = ContextVar("current_database_target", default=None)


def set_current_identity(identity: AuthenticatedIdentity | None) -> Token:
    return _current_identity.set(identity)


def reset_current_identity(token: Token) -> None:
    _current_identity.reset(token)


def get_current_identity() -> AuthenticatedIdentity | None:
    return _current_identity.get()


def set_current_bearer_token(bearer_token: str | None) -> Token:
    return _current_bearer_token.set(bearer_token)


def reset_current_bearer_token(token: Token) -> None:
    _current_bearer_token.reset(token)


def get_current_bearer_token() -> str | None:
    return _current_bearer_token.get()


def set_current_database_target(target: str | None) -> Token:
    """Set a validated database target for this request/task context."""
    value = str(target or "").strip()
    if value not in {"Test_OperationsDB", "OperationsDB"}:
        value = None
    return _current_database_target.set(value)


def reset_current_database_target(token: Token) -> None:
    _current_database_target.reset(token)


def get_current_database_target() -> str | None:
    return _current_database_target.get()
