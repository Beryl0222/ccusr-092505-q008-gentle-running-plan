"""测试共用构造器。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gentle_running_plan import (
    FixedClock,
    HealthProfile,
    Interval,
    PrescriptionEngine,
    SessionSpec,
)

T0 = "2026-10-06T08:00:00+08:00"


def make_engine(now: str = T0) -> PrescriptionEngine:
    return PrescriptionEngine(clock=FixedClock(now))


def make_plan(
    n: int = 4,
    *,
    jog_seconds: int = 60,
    walk_seconds: int = 120,
    repeats: int = 3,
    max_hr: int = 91,
    start_day: int = 7,
) -> list[SessionSpec]:
    sessions: list[SessionSpec] = []
    for i in range(n):
        day = start_day + i * 2
        sessions.append(SessionSpec(
            seq=i + 1,
            scheduled_at=f"2026-10-{day:02d}T08:00:00+08:00",
            warmup_minutes=5,
            intervals=(
                Interval("walk", walk_seconds, repeats),
                Interval("slow_jog", jog_seconds, repeats),
            ),
            cooldown_minutes=5,
            targets={"max_heart_rate": max_hr, "max_rpe": 3},
        ))
    return sessions


def enrolled_engine(
    participant_id: str = "p1",
    *,
    now: str = T0,
    profile_kwargs: dict | None = None,
    sessions: list[SessionSpec] | None = None,
) -> PrescriptionEngine:
    engine = make_engine(now)
    kwargs = {"participant_id": participant_id, "age": 68}
    kwargs.update(profile_kwargs or {})
    engine.screening.submit(HealthProfile(**kwargs))
    engine.plans.publish(
        participant_id, sessions or make_plan(), reason="初版处方"
    )
    return engine
