from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from leadguard.domain import Action, Intent, LeakageReview, ModelDecision
from leadguard.llm import DialogueTurn
from leadguard.output_guard import OutputGuard
from leadguard.service import AgentService
from leadguard.storage import SQLiteStore

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(slots=True)
class FakeClock:
    current: datetime = BASE_TIME

    def now(self) -> datetime:
        return self.current

    def set(self, value: datetime) -> None:
        self.current = value

    def advance(self, **delta: float) -> None:
        self.current += timedelta(**delta)


DecisionStep = ModelDecision | Exception
ReviewStep = LeakageReview | Exception


class ScriptedLLM:
    """Deterministic LLM double that makes every unexpected call a test failure."""

    def __init__(
        self,
        decisions: Iterable[DecisionStep] = (),
        reviews: Iterable[ReviewStep] = (),
        *,
        default_review: LeakageReview | None = None,
    ) -> None:
        self._decisions = deque(decisions)
        self._reviews = deque(reviews)
        self._default_review = default_review
        self.decide_calls: list[str] = []
        self.decide_histories: list[tuple[DialogueTurn, ...]] = []
        self.review_calls: list[tuple[str, str]] = []
        self.close_calls = 0

    async def decide(
        self,
        customer_message: str,
        *,
        history: tuple[DialogueTurn, ...] = (),
    ) -> ModelDecision:
        self.decide_calls.append(customer_message)
        self.decide_histories.append(history)
        if not self._decisions:
            raise AssertionError(f"unexpected decide call: {customer_message!r}")
        step = self._decisions.popleft()
        if isinstance(step, Exception):
            raise step
        return step

    async def review_reply(self, customer_message: str, reply_draft: str) -> LeakageReview:
        self.review_calls.append((customer_message, reply_draft))
        if self._reviews:
            step = self._reviews.popleft()
        elif self._default_review is not None:
            step = self._default_review
        else:
            raise AssertionError(f"unexpected review call: {reply_draft!r}")
        if isinstance(step, Exception):
            raise step
        return step

    async def aclose(self) -> None:
        self.close_calls += 1


class BarrierLLM(ScriptedLLM):
    """Stops decisions after all expected callers enter, exposing real races."""

    def __init__(
        self,
        decisions: Iterable[DecisionStep],
        *,
        expected_callers: int,
        default_review: LeakageReview | None = None,
    ) -> None:
        super().__init__(decisions, default_review=default_review)
        self.expected_callers = expected_callers
        self.arrived = 0
        self.all_arrived = asyncio.Event()
        self.release = asyncio.Event()

    async def decide(
        self,
        customer_message: str,
        *,
        history: tuple[DialogueTurn, ...] = (),
    ) -> ModelDecision:
        self.decide_calls.append(customer_message)
        self.decide_histories.append(history)
        if not self._decisions:
            raise AssertionError(f"unexpected decide call: {customer_message!r}")
        step = self._decisions.popleft()
        self.arrived += 1
        if self.arrived >= self.expected_callers:
            self.all_arrived.set()
        await self.release.wait()
        if isinstance(step, Exception):
            raise step
        return step


class DelayedScriptedLLM(ScriptedLLM):
    """Returns message-specific decisions after controlled, unequal delays."""

    def __init__(
        self,
        decisions: Mapping[str, DecisionStep],
        delays: Mapping[str, float],
        *,
        default_review: LeakageReview | None = None,
    ) -> None:
        super().__init__(default_review=default_review)
        self._decisions_by_message = dict(decisions)
        self._delays = dict(delays)
        self.started: asyncio.Queue[str] = asyncio.Queue()
        self.decide_completions: list[str] = []

    async def decide(
        self,
        customer_message: str,
        *,
        history: tuple[DialogueTurn, ...] = (),
    ) -> ModelDecision:
        self.decide_calls.append(customer_message)
        self.decide_histories.append(history)
        await self.started.put(customer_message)
        await asyncio.sleep(self._delays.get(customer_message, 0))
        try:
            step = self._decisions_by_message.pop(customer_message)
        except KeyError as error:
            raise AssertionError(f"unexpected decide call: {customer_message!r}") from error
        self.decide_completions.append(customer_message)
        if isinstance(step, Exception):
            raise step
        return step


@dataclass(frozen=True, slots=True)
class ServiceHarness:
    store: SQLiteStore
    llm: ScriptedLLM
    clock: FakeClock
    service: AgentService

    def create_customer(self, customer_id: str, name: str | None = None) -> str:
        self.store.create_conversation(
            name or customer_id,
            self.clock.now(),
            conversation_id=customer_id,
        )
        return customer_id


def safe_review() -> LeakageReview:
    return LeakageReview(safe=True, category="public_sales_reply", rationale="public only")


def model_decision(
    *,
    intent: Intent = Intent.NEEDS_MORE_INFO,
    dissatisfied: bool = False,
    action: Action = Action.REPLY,
    reply: str = "可以，我来介绍公开的产品能力。",
    rationale: str = "test decision",
) -> ModelDecision:
    return ModelDecision(
        intent=intent,
        dissatisfied=dissatisfied,
        action=action,
        reply_draft=reply if action is Action.REPLY else None,
        rationale=rationale,
    )


def build_harness(
    tmp_path: Path,
    llm: ScriptedLLM,
    *,
    clock: FakeClock | None = None,
    database_name: str = "leadguard-test.db",
) -> ServiceHarness:
    runtime_clock = clock or FakeClock()
    store = SQLiteStore(tmp_path / database_name, rate_limit_seconds=60)
    store.initialize()
    service = AgentService(
        store=store,
        llm=llm,
        clock=runtime_clock,
        output_guard=OutputGuard(llm, max_reply_chars=320),
    )
    return ServiceHarness(store=store, llm=llm, clock=runtime_clock, service=service)


def agent_messages(store: SQLiteStore, customer_id: str) -> list[str]:
    return [
        item["content"] for item in store.list_messages(customer_id) if item["sender"] == "agent"
    ]
