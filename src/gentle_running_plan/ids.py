"""聚合流标识约定。"""

from __future__ import annotations


def participant_stream(participant_id: str) -> str:
    return f"participant:{participant_id}"


def plan_stream(participant_id: str) -> str:
    return f"plan:{participant_id}"


def safety_stream(participant_id: str) -> str:
    return f"safety:{participant_id}"


def session_stream(participant_id: str, seq: int) -> str:
    return f"session:{participant_id}:{seq}"


def reminder_stream(reminder_id: str) -> str:
    return f"reminder:{reminder_id}"


def session_reminder_id(participant_id: str, plan_version: int, seq: int) -> str:
    return f"reminder:{participant_id}:v{plan_version}:s{seq}"
