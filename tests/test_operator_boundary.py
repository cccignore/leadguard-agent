"""Operator control-plane boundary and interrupted-request recovery."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from conftest import (
    FakeClock,
    ScriptedLLM,
    build_harness,
    model_decision,
    safe_review,
)
from fastapi.testclient import TestClient
from pydantic import SecretStr

from leadguard.app import create_app
from leadguard.config import Settings
from leadguard.domain import Action, Intent

OPERATOR_HEADER = {"X-Operator-Token": "unit-operator-secret"}


def _enforced_app(tmp_path, llm: ScriptedLLM):
    settings = Settings(
        database_path=tmp_path / "operator.db",
        operator_token=SecretStr("unit-operator-secret"),
    )
    return create_app(settings, llm=llm, clock=FakeClock())


def test_control_plane_requires_operator_token(tmp_path) -> None:
    llm = ScriptedLLM(
        [
            model_decision(
                intent=Intent.OTHER,
                action=Action.ESCALATE_TO_HUMAN,
                reply="ignored",
            )
        ],
        default_review=safe_review(),
    )
    app = _enforced_app(tmp_path, llm)

    with TestClient(app) as client:
        assert client.get("/api/health").json()["operator_auth"] == "enforced"

        # Diagnostics and control-plane routes are closed to anonymous callers.
        assert client.get("/api/conversations").status_code == 401
        listed = client.get("/api/conversations", headers=OPERATOR_HEADER)
        assert listed.status_code == 200
        customer_id = listed.json()[0]["id"]
        assert client.get(f"/api/conversations/{customer_id}").status_code == 401
        assert client.post("/api/demo/reset").status_code == 401

        # The customer plane still works, and the conversation escalates.
        message = client.post(
            f"/api/conversations/{customer_id}/messages",
            json={"content": "请转人工", "request_id": str(uuid4())},
        )
        assert message.status_code == 200

        # Customer text cannot flip the agent back on: no token, no reactivate.
        assert (
            client.post(f"/api/conversations/{customer_id}/reactivate").status_code
            == 401
        )
        wrong = client.post(
            f"/api/conversations/{customer_id}/reactivate",
            headers={"X-Operator-Token": "guessed"},
        )
        assert wrong.status_code == 401
        state = client.get(
            f"/api/conversations/{customer_id}", headers=OPERATOR_HEADER
        ).json()["conversation"]
        assert state["lifecycle"] == "human_controlled"

        restored = client.post(
            f"/api/conversations/{customer_id}/reactivate", headers=OPERATOR_HEADER
        )
        assert restored.status_code == 200
        assert restored.json()["lifecycle"] == "active"


def test_customer_plane_sees_public_view_operator_sees_diagnostics(tmp_path) -> None:
    llm = ScriptedLLM(
        [
            model_decision(reply="公开回复内容"),
            model_decision(reply="第二条公开回复"),
        ],
        default_review=safe_review(),
    )
    app = _enforced_app(tmp_path, llm)

    with TestClient(app) as client:
        customer_id = client.get(
            "/api/conversations", headers=OPERATOR_HEADER
        ).json()[0]["id"]

        public = client.post(
            f"/api/conversations/{customer_id}/messages",
            json={"content": "介绍一下你们的产品", "request_id": str(uuid4())},
        ).json()
        assert set(public) == {"request_id", "outcome", "reply"}
        assert public["outcome"] == "sent"
        assert public["reply"] == "公开回复内容"

        clock: FakeClock = app.state.service.clock  # advance past the rate limit
        clock.advance(seconds=61)
        operator = client.post(
            f"/api/conversations/{customer_id}/messages",
            json={"content": "再介绍一次", "request_id": str(uuid4())},
            headers=OPERATOR_HEADER,
        ).json()
        assert operator["outcome"] == "sent"
        assert "guard_events" in operator
        assert "classification" in operator
        assert "strike_count" in operator


def test_interrupted_turn_can_be_retried_with_same_request_id(tmp_path) -> None:
    llm = ScriptedLLM([model_decision(reply="终于处理完成")], default_review=safe_review())
    harness = build_harness(tmp_path, llm)
    customer = harness.create_customer("crash-customer")
    request_id = str(uuid4())

    # Simulate a crash after the inbound write but before any turn result:
    # the message row exists, the turn does not.
    harness.store.record_inbound(customer, request_id, "断电前的消息", harness.clock.now())
    assert harness.store.get_turn_result(customer, request_id) is None

    result = asyncio.run(
        harness.service.process_message(customer, request_id, "断电前的消息")
    )
    assert result.outcome.value == "sent"
    assert llm.decide_calls == ["断电前的消息"]
    assert harness.store.get_turn_result(customer, request_id) is not None
    # The retry did not duplicate the inbound message row.
    customer_rows = [
        item
        for item in harness.store.list_messages(customer)
        if item["sender"] == "customer"
    ]
    assert len(customer_rows) == 1


def test_request_id_reuse_with_different_content_is_rejected(tmp_path) -> None:
    llm = ScriptedLLM([model_decision(reply="第一次回复")], default_review=safe_review())
    settings = Settings(database_path=tmp_path / "mismatch.db")
    app = create_app(settings, llm=llm, clock=FakeClock())

    with TestClient(app) as client:
        customer_id = client.get("/api/conversations").json()[0]["id"]
        request_id = str(uuid4())
        first = client.post(
            f"/api/conversations/{customer_id}/messages",
            json={"content": "原始内容", "request_id": request_id},
        )
        assert first.status_code == 200

        mismatch = client.post(
            f"/api/conversations/{customer_id}/messages",
            json={"content": "同一个 request_id 却换了内容", "request_id": request_id},
        )
        assert mismatch.status_code == 409
        assert len(llm.decide_calls) == 1
