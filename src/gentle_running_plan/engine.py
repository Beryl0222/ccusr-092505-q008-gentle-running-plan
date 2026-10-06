"""引擎装配：把事件存储、可注入日历与各领域服务组装成一个入口。"""

from __future__ import annotations

from pathlib import Path

from .batch import BatchPlanGenerator
from .clock import Clock, SystemClock
from .explain import ExplanationService
from .ingest import IngestService
from .plan import PlanService
from .reminders import NotificationSender, ReminderService
from .safety import SafetyService
from .screening import ScreeningService
from .store import EventStore
from .views import Roster, ViewService


class PrescriptionEngine:
    """个体处方引擎门面。

    >>> engine = PrescriptionEngine()  # 内存模式
    >>> engine.screening, engine.plans  # doctest: +ELLIPSIS
    (<...ScreeningService...>, <...PlanService...>)
    """

    def __init__(
        self,
        store: EventStore | str | Path | None = None,
        *,
        clock: Clock | None = None,
        roster: Roster | None = None,
        sender: NotificationSender | None = None,
    ) -> None:
        self.clock = clock or SystemClock()
        self.store = (
            store
            if isinstance(store, EventStore)
            else EventStore(store)
        )
        self.roster = roster or Roster()
        self.screening = ScreeningService(self.store, self.clock)
        self.safety = SafetyService(self.store, self.clock)
        self.plans = PlanService(self.store, self.clock)
        self.ingest = IngestService(self.store, self.plans, self.safety, self.clock)
        self.batch = BatchPlanGenerator(self.plans, self.clock)
        self.reminders = ReminderService(self.store, self.clock, sender=sender)
        self.views = ViewService(self.store, self.roster)
        self.explain = ExplanationService(self.store)
