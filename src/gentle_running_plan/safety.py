"""安全门槛与暂停/恢复判定。

危险症状或超出授权负荷时立即暂停后续训练；
只有暂停之后由专业人员出具的新复核意见才能恢复。
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .model import DANGER_SYMPTOMS, MedicalClearance, PauseRecord, SafetyReport, SessionRecord


def danger_symptoms_in(symptoms: Iterable[str]) -> tuple[str, ...]:
    return tuple(s for s in symptoms if s in DANGER_SYMPTOMS)


def evaluate_record(
    record: SessionRecord, clearance: MedicalClearance | None, now: datetime
) -> PauseRecord | None:
    hit = danger_symptoms_in(record.symptoms)
    if hit:
        return PauseRecord(
            record.participant_id,
            now,
            "danger_symptom",
            f"训练中出现危险症状（{'、'.join(hit)}），已暂停后续训练，待专业复核",
            (record.record_id,),
        )
    if (
        clearance is not None
        and clearance.max_heart_rate is not None
        and record.max_heart_rate is not None
        and record.max_heart_rate > clearance.max_heart_rate
    ):
        return PauseRecord(
            record.participant_id,
            now,
            "load_exceeds_authorization",
            f"最高心率 {record.max_heart_rate} 超出授权上限 {clearance.max_heart_rate}，"
            "已暂停后续训练，待专业复核",
            (record.record_id, clearance.clearance_id),
        )
    return None


def evaluate_report(report: SafetyReport, now: datetime) -> PauseRecord | None:
    hit = danger_symptoms_in(report.symptoms)
    if hit:
        return PauseRecord(
            report.participant_id,
            now,
            "danger_symptom",
            f"异常报告出现危险症状（{'、'.join(hit)}），已暂停后续训练，待专业复核",
            (report.report_id,),
        )
    return None


def can_resume(pause: PauseRecord, clearance: MedicalClearance) -> bool:
    """恢复条件：同一学员、明确允许恢复、有复核人、且复核时间晚于暂停时间。"""
    return (
        clearance.participant_id == pause.participant_id
        and clearance.resumes_training
        and bool(clearance.reviewer.strip())
        and clearance.recorded_at > pause.paused_at
    )
