"""训练记录的只追加存储：幂等去重、冲突转复核、原始体征不可改写。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .model import ConflictCase, SessionRecord

_VALUE_FIELDS = ("avg_heart_rate", "max_heart_rate", "rpe", "completed_minutes", "symptoms")


@dataclass(frozen=True)
class IngestResult:
    record_id: str
    outcome: str  # stored | duplicate | conflict
    conflict: ConflictCase | None = None


class RecordStore:
    """只追加存储。

    - 同一 record_id 重复上报（离线设备重试）：幂等忽略。
    - 同一学员同一时段再次上报且数值一致：视为重复，只保留一条有效记录。
    - 同一时段数值冲突：保留先到的有效记录，冲突方转入复核队列，绝不取平均。
    - 所有原始记录（包括冲突方）都完整保留，任何复核结论都以新数据追加。
    """

    def __init__(self) -> None:
        self._records: list[SessionRecord] = []
        self._by_id: dict[str, SessionRecord] = {}
        self._effective_by_slot: dict[tuple[str, datetime], SessionRecord] = {}
        self._conflicts: list[ConflictCase] = []

    def ingest(self, record: SessionRecord) -> IngestResult:
        if record.record_id in self._by_id:
            return IngestResult(record.record_id, "duplicate")
        key = (record.participant_id, record.scheduled_slot)
        existing = self._effective_by_slot.get(key)
        self._records.append(record)
        self._by_id[record.record_id] = record
        if existing is None:
            self._effective_by_slot[key] = record
            return IngestResult(record.record_id, "stored")
        differing = tuple(
            field for field in _VALUE_FIELDS if getattr(existing, field) != getattr(record, field)
        )
        if not differing:
            return IngestResult(record.record_id, "duplicate")
        conflict = ConflictCase(
            record.participant_id,
            record.scheduled_slot,
            existing.record_id,
            record.record_id,
            differing,
        )
        self._conflicts.append(conflict)
        return IngestResult(record.record_id, "conflict", conflict)

    def all_records(self, participant_id: str) -> list[SessionRecord]:
        """含冲突方在内的全部原始记录，按到达顺序。"""
        return [r for r in self._records if r.participant_id == participant_id]

    def effective_records(self, participant_id: str) -> list[SessionRecord]:
        """每个时段一条的有效记录。"""
        return [
            record
            for (pid, _), record in self._effective_by_slot.items()
            if pid == participant_id
        ]

    def completed_session_indices(self, participant_id: str) -> set[int]:
        return {r.session_index for r in self.effective_records(participant_id)}

    def conflicts(self, participant_id: str | None = None) -> list[ConflictCase]:
        if participant_id is None:
            return list(self._conflicts)
        return [c for c in self._conflicts if c.participant_id == participant_id]
