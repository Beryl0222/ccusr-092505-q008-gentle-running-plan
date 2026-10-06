"""批量生成计划：单个学员失败不影响整体，失败片段可单独重试。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

from .engine import PrescriptionEngine
from .model import PlanVersion


@dataclass
class BatchResult:
    generated: dict[str, PlanVersion] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failures


def generate_batch(
    engine: PrescriptionEngine,
    participant_ids: Iterable[str],
    start_date: date,
    weeks: int = 4,
) -> BatchResult:
    result = BatchResult()
    for pid in participant_ids:
        try:
            result.generated[pid] = engine.generate_plan(pid, start_date, weeks=weeks)
        except Exception as exc:  # 单个片段失败不中断整个批次
            result.failures[pid] = str(exc)
    return result


def retry_failed(
    engine: PrescriptionEngine,
    previous: BatchResult,
    start_date: date,
    weeks: int = 4,
) -> BatchResult:
    """只重试上一批次的失败片段，已成功的结果原样保留。"""
    retry = generate_batch(engine, list(previous.failures), start_date, weeks=weeks)
    return BatchResult(
        generated={**previous.generated, **retry.generated},
        failures=retry.failures,
    )
