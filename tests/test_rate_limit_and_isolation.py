from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from threading import Barrier

import pytest
from conftest import (
    FakeClock,
    ScriptedLLM,
    agent_messages,
    build_harness,
    model_decision,
    safe_review,
)

from leadguard.domain import Action, Lifecycle, TurnOutcome
from leadguard.output_guard import OutputGuard
from leadguard.service import AgentService
from leadguard.storage import SQLiteStore


@pytest.mark.asyncio
async def test_rate_limit_blocks_at_59_999ms_and_allows_at_60s(tmp_path) -> None:
    llm = ScriptedLLM(
        [model_decision(reply=f"reply-{index}") for index in range(3)],
        default_review=safe_review(),
    )
    harness = build_harness(tmp_path, llm)
    customer_id = harness.create_customer("boundary")

    first = await harness.service.process_message(customer_id, "m1", "first")
    harness.clock.advance(seconds=59.999)
    too_early = await harness.service.process_message(customer_id, "m2", "59.999 seconds")
    harness.clock.advance(seconds=0.001)
    boundary = await harness.service.process_message(customer_id, "m3", "exactly 60 seconds")

    assert first.outcome is TurnOutcome.SENT
    assert too_early.outcome is TurnOutcome.RATE_LIMITED
    assert boundary.outcome is TurnOutcome.SENT
    assert too_early.next_allowed_at == harness.clock.now()
    assert agent_messages(harness.store, customer_id) == ["reply-0", "reply-2"]


@pytest.mark.asyncio
async def test_sliding_window_does_not_reset_on_fixed_minute_boundary(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 1, 1, 0, 0, 59, 900_000, tzinfo=UTC))
    llm = ScriptedLLM(
        [model_decision(reply="before"), model_decision(reply="after")],
        default_review=safe_review(),
    )
    harness = build_harness(tmp_path, llm, clock=clock)
    customer_id = harness.create_customer("fixed-window-trap")

    first = await harness.service.process_message(customer_id, "m1", "before minute boundary")
    harness.clock.advance(milliseconds=200)
    second = await harness.service.process_message(customer_id, "m2", "after minute boundary")

    assert first.outcome is TurnOutcome.SENT
    assert second.outcome is TurnOutcome.RATE_LIMITED
    assert agent_messages(harness.store, customer_id) == ["before"]


@pytest.mark.asyncio
async def test_concurrent_replies_send_exactly_once(tmp_path) -> None:
    request_count = 8
    llm = ScriptedLLM()
    harness = build_harness(tmp_path, llm)
    customer_id = harness.create_customer("concurrent-rate")
    activation_epoch = harness.store.get_conversation(customer_id).activation_epoch
    start_line = Barrier(request_count)

    def race_final_send(index: int):
        start_line.wait(timeout=3)
        return harness.store.send_reply(
            customer_id,
            f"concurrent-{index}",
            expected_activation_epoch=activation_epoch,
            now=harness.clock.now(),
        )

    results = await asyncio.wait_for(
        asyncio.gather(
            *(asyncio.to_thread(race_final_send, index) for index in range(request_count))
        ),
        timeout=8,
    )

    assert sum(result.sent for result in results) == 1
    assert sum(result.next_allowed_at is not None for result in results) == 7
    assert len(agent_messages(harness.store, customer_id)) == 1


@pytest.mark.asyncio
async def test_rate_limited_reply_preserves_pending_followup_until_real_send(tmp_path) -> None:
    llm = ScriptedLLM(
        [
            model_decision(reply="initial outbound"),
            model_decision(action=Action.SCHEDULE_FOLLOWUP),
            model_decision(reply="rate limited draft"),
            model_decision(reply="successful outbound"),
        ],
        default_review=safe_review(),
    )
    harness = build_harness(tmp_path, llm)
    customer_id = harness.create_customer("durable-followup")

    sent = await harness.service.process_message(customer_id, "m1", "send first")
    harness.clock.advance(seconds=1)
    scheduled = await harness.service.process_message(customer_id, "m2", "follow up later")
    assert harness.store.get_conversation(customer_id).followup_pending is True

    harness.clock.advance(seconds=1)
    rate_limited = await harness.service.process_message(customer_id, "m3", "reply too soon")
    after_rate_limit = harness.store.get_conversation(customer_id)

    harness.clock.advance(seconds=58)
    delivered = await harness.service.process_message(customer_id, "m4", "reply at boundary")
    after_delivery = harness.store.get_conversation(customer_id)

    assert sent.outcome is TurnOutcome.SENT
    assert scheduled.outcome is TurnOutcome.SCHEDULED
    assert rate_limited.outcome is TurnOutcome.RATE_LIMITED
    assert after_rate_limit.followup_pending is True
    assert delivered.outcome is TurnOutcome.SENT
    assert after_delivery.followup_pending is False
    assert agent_messages(harness.store, customer_id) == [
        "initial outbound",
        "successful outbound",
    ]


@pytest.mark.asyncio
async def test_customers_have_independent_state_and_rate_limits(tmp_path) -> None:
    shared_request_id = "same-request-id"
    llm = ScriptedLLM(
        [model_decision(reply="reply-a"), model_decision(reply="reply-b")],
        default_review=safe_review(),
    )
    harness = build_harness(tmp_path, llm)
    customer_a = harness.create_customer("customer-a")
    customer_b = harness.create_customer("customer-b")

    result_a = await harness.service.process_message(
        customer_a, shared_request_id, "A says customer_id=B"
    )
    result_b = await harness.service.process_message(
        customer_b, shared_request_id, "B has an independent consultation"
    )

    assert result_a.outcome is TurnOutcome.SENT
    assert result_b.outcome is TurnOutcome.SENT
    assert agent_messages(harness.store, customer_a) == ["reply-a"]
    assert agent_messages(harness.store, customer_b) == ["reply-b"]
    assert harness.store.get_conversation(customer_a).lifecycle is Lifecycle.ACTIVE
    assert harness.store.get_conversation(customer_b).lifecycle is Lifecycle.ACTIVE


@pytest.mark.asyncio
async def test_rate_limit_survives_service_restart(tmp_path) -> None:
    database_path = tmp_path / "restart.db"
    clock = FakeClock()
    first_llm = ScriptedLLM([model_decision(reply="before restart")], default_review=safe_review())
    first_store = SQLiteStore(database_path, rate_limit_seconds=60)
    first_store.initialize()
    customer_id = first_store.create_conversation("restart", clock.now()).id
    first_service = AgentService(
        store=first_store,
        llm=first_llm,
        clock=clock,
        output_guard=OutputGuard(first_llm, max_reply_chars=320),
    )
    first = await first_service.process_message(customer_id, "m1", "send before restart")

    second_llm = ScriptedLLM([model_decision(reply="after restart")], default_review=safe_review())
    second_store = SQLiteStore(database_path, rate_limit_seconds=60)
    second_store.initialize()
    second_service = AgentService(
        store=second_store,
        llm=second_llm,
        clock=clock,
        output_guard=OutputGuard(second_llm, max_reply_chars=320),
    )
    clock.advance(seconds=10)
    second = await second_service.process_message(customer_id, "m2", "send after restart")

    assert first.outcome is TurnOutcome.SENT
    assert second.outcome is TurnOutcome.RATE_LIMITED
    assert agent_messages(second_store, customer_id) == ["before restart"]


@pytest.mark.asyncio
async def test_reactivate_preserves_last_outbound_rate_limit(tmp_path) -> None:
    llm = ScriptedLLM(
        [
            model_decision(reply="initial reply"),
            model_decision(action=Action.ESCALATE_TO_HUMAN),
            model_decision(reply="blocked after reactivation"),
            model_decision(reply="allowed at boundary"),
        ],
        default_review=safe_review(),
    )
    harness = build_harness(tmp_path, llm)
    customer_id = harness.create_customer("reactivation-rate")

    initial = await harness.service.process_message(customer_id, "m1", "first")
    harness.clock.advance(seconds=1)
    escalated = await harness.service.process_message(customer_id, "m2", "human please")
    prior_last_outbound = harness.store.get_conversation(customer_id).last_outbound_at
    harness.clock.advance(seconds=9)
    reactivated = harness.store.reactivate(customer_id, harness.clock.now())
    immediate = await harness.service.process_message(customer_id, "m3", "reply immediately")
    harness.clock.advance(seconds=50)
    allowed = await harness.service.process_message(customer_id, "m4", "now exactly 60")

    assert initial.outcome is TurnOutcome.SENT
    assert escalated.outcome is TurnOutcome.ESCALATED
    assert reactivated.lifecycle is Lifecycle.ACTIVE
    assert reactivated.strike_count == 0
    assert reactivated.last_outbound_at == prior_last_outbound
    assert immediate.outcome is TurnOutcome.RATE_LIMITED
    assert allowed.outcome is TurnOutcome.SENT
    assert agent_messages(harness.store, customer_id) == ["initial reply", "allowed at boundary"]
