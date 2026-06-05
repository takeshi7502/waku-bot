from __future__ import annotations

from dataclasses import dataclass
from time import monotonic


@dataclass
class GroupMemberOperation:
    chat_id: int
    kind: str
    owner_id: int | None = None
    started_at: float = 0.0
    stop_requested: bool = False


_GROUP_MEMBER_OPS: dict[int, GroupMemberOperation] = {}


def acquire_group_member_operation(chat_id: int, kind: str, owner_id: int | None = None) -> GroupMemberOperation | None:
    existing = _GROUP_MEMBER_OPS.get(chat_id)
    if existing is not None:
        return None
    operation = GroupMemberOperation(chat_id=chat_id, kind=kind, owner_id=owner_id, started_at=monotonic())
    _GROUP_MEMBER_OPS[chat_id] = operation
    return operation


def get_group_member_operation(chat_id: int) -> GroupMemberOperation | None:
    return _GROUP_MEMBER_OPS.get(chat_id)


def request_group_member_operation_stop(chat_id: int, kind: str | None = None) -> GroupMemberOperation | None:
    operation = _GROUP_MEMBER_OPS.get(chat_id)
    if operation is None:
        return None
    if kind is not None and operation.kind != kind:
        return None
    operation.stop_requested = True
    return operation


def release_group_member_operation(chat_id: int, operation: GroupMemberOperation | None = None) -> None:
    if operation is not None and _GROUP_MEMBER_OPS.get(chat_id) is not operation:
        return
    _GROUP_MEMBER_OPS.pop(chat_id, None)


def describe_group_member_operation(operation: GroupMemberOperation | None) -> str:
    if operation is None:
        return "none"
    return operation.kind.replace("_", " ")
