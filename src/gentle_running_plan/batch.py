"""批量生成计划：片段隔离、失败可重试。

每个参与者是一个独立片段：单条失败（未筛查、超授权负荷、暂停中……）不影响
其他片段；重试时只重放失败片段，成功片段不会重复建计划。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .clock import Clock, SystemClock
from .plan import PlanError, PlanService, SessionSpec


@dataclass(frozen=True)
class PlanFragment:
    fragment_id: str
    participant_id: str
    sessions: Sequence[SessionSpec]
    reason: str = "批量初版处方"


@dataclass(frozen=True)
class FragmentOutcome:
    fragment_id: str
    participant_id: str
    ok: bool
    event_id: str | None = None
    error: str | None = None


@dataclass
class BatchResult:
    outcomes: dict[str, FragmentOutcome] = field(default_factory=dict)
    fragments: dict[str, PlanFragment] = field(default_factory=dict)

    @property
    def succeeded(self) -> list[FragmentOutcome]:
        return [o for o in self.outcomes.values() if o.ok]

    @property
    def failed(self) -> list[FragmentOutcome]:
        return [o for o in self.outcomes.values() if not o.ok]

    @property
    def all_succeeded(self) -> bool:
        return not self.failed

    def failed_fragments(
        self, overrides: Mapping[str, Sequence[SessionSpec]] | None = None
    ) -> list[PlanFragment]:
        """从失败结果构造重试输入；可用 overrides 替换课表。"""
        fragments: list[PlanFragment] = []
        for outcome in self.failed:
            original = self.fragments[outcome.fragment_id]
            fragments.append(PlanFragment(
                fragment_id=original.fragment_id,
                participant_id=original.participant_id,
                sessions=(overrides or {}).get(
                    outcome.fragment_id, original.sessions
                ),
                reason=original.reason,
            ))
        return fragments


class BatchPlanGenerator:
    def __init__(self, plans: PlanService, clock: Clock | None = None) -> None:
        self.plans = plans
        self.clock = clock or SystemClock()

    def generate(self, fragments: Sequence[PlanFragment]) -> BatchResult:
        result = BatchResult(
            fragments={f.fragment_id: f for f in fragments}
        )
        for fragment in fragments:
            result.outcomes[fragment.fragment_id] = self._run(fragment)
        return result

    def retry(
        self,
        result: BatchResult,
        *,
        overrides: Mapping[str, Sequence[SessionSpec]] | None = None,
    ) -> BatchResult:
        """只重放失败片段；成功结果原样保留。"""
        retried = self.generate(result.failed_fragments(overrides))
        merged = BatchResult()
        merged.outcomes.update(result.outcomes)
        merged.outcomes.update(retried.outcomes)
        merged.fragments = {**result.fragments, **retried.fragments}
        return merged

    def _run(self, fragment: PlanFragment) -> FragmentOutcome:
        try:
            event = self.plans.publish(
                fragment.participant_id,
                fragment.sessions,
                reason=fragment.reason,
            )
        except (PlanError, ValueError) as exc:
            return FragmentOutcome(
                fragment_id=fragment.fragment_id,
                participant_id=fragment.participant_id,
                ok=False,
                error=str(exc),
            )
        return FragmentOutcome(
            fragment_id=fragment.fragment_id,
            participant_id=fragment.participant_id,
            ok=True,
            event_id=event.event_id,
        )
