from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from leadguard.config import Settings
from leadguard.domain import Action, Intent, LeakageReview, ModelDecision
from leadguard.evaluation import (
    ERROR_LABEL,
    EvalCase,
    EvalObservation,
    compute_metrics,
    load_cases,
    render_markdown_report,
    run_evaluation,
    sanitized_results_payload,
)


def _case(
    case_id: str,
    intent: Intent,
    *,
    accepted: list[Intent] | None = None,
    dissatisfied: bool = False,
    actions: list[Action] | None = None,
    message: str = "public eval message",
) -> EvalCase:
    return EvalCase(
        id=case_id,
        language="zh",
        message=message,
        history=[],
        canonical_intent=intent,
        accepted_intents=accepted or [intent],
        dissatisfied=dissatisfied,
        accepted_actions=actions or [Action.REPLY],
        tags=["unit"],
    )


def _observation(
    case_id: str,
    repeat: int,
    *,
    intent: Intent | None = None,
    dissatisfied: bool | None = None,
    action: Action | None = None,
    latency_ms: float = 10,
    error_kind: str | None = None,
) -> EvalObservation:
    return EvalObservation(
        case_id=case_id,
        repeat=repeat,
        predicted_intent=intent,
        predicted_dissatisfied=dissatisfied,
        predicted_action=action,
        latency_ms=latency_ms,
        error_kind=error_kind,
    )


def test_committed_dataset_is_balanced_and_valid() -> None:
    cases = load_cases(Path("evals/model_cases.jsonl"))

    assert len(cases) == 60
    assert len({case.id for case in cases}) == 60
    assert Counter(case.canonical_intent for case in cases) == {intent: 12 for intent in Intent}
    for intent in Intent:
        subset = [case for case in cases if case.canonical_intent is intent]
        assert sum(case.dissatisfied for case in subset) == 3
        assert Counter(case.language for case in subset) == {
            "zh": 9,
            "en": 1,
            "ja": 1,
            "es": 1,
        }


def test_dataset_loader_reports_line_and_duplicate_without_raw_content(
    tmp_path: Path,
) -> None:
    invalid = tmp_path / "invalid.jsonl"
    fake_secret = "sk-FAKE-DATASET-SECRET"
    invalid.write_text(
        json.dumps(
            {
                "id": "BAD-1",
                "language": "zh",
                "message": fake_secret,
                "history": [],
                "canonical_intent": "interested",
                "accepted_intents": ["interested"],
                "dissatisfied": False,
                "accepted_actions": ["reply"],
                "tags": ["unit"],
                "unexpected": True,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as captured:
        load_cases(invalid)
    assert "line 1" in str(captured.value)
    assert fake_secret not in str(captured.value)

    valid_line = Path("evals/model_cases.jsonl").read_text(encoding="utf-8").splitlines()[0]
    duplicate = tmp_path / "duplicate.jsonl"
    duplicate.write_text(valid_line + "\n" + valid_line + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate evaluation case id at line 2"):
        load_cases(duplicate)

    with pytest.raises(ValidationError, match="exactly one accepted action"):
        _case(
            "MULTI-ACTION",
            Intent.OTHER,
            actions=[Action.REPLY, Action.SCHEDULE_FOLLOWUP],
        )


def test_metrics_keep_errors_in_denominators_and_track_accepted_boundaries() -> None:
    cases = [
        _case("C-I", Intent.INTERESTED),
        _case(
            "C-N",
            Intent.NEEDS_MORE_INFO,
            accepted=[Intent.NEEDS_MORE_INFO, Intent.INTERESTED],
        ),
        _case(
            "C-R",
            Intent.REJECTED,
            dissatisfied=True,
            actions=[Action.MARK_NOT_INTERESTED],
        ),
        _case("C-O", Intent.OFF_TOPIC, dissatisfied=True),
        _case("C-T", Intent.OTHER, actions=[Action.SCHEDULE_FOLLOWUP]),
    ]
    observations = [
        _observation(
            "C-I",
            1,
            intent=Intent.INTERESTED,
            dissatisfied=False,
            action=Action.REPLY,
            latency_ms=10,
        ),
        _observation(
            "C-N",
            1,
            intent=Intent.INTERESTED,
            dissatisfied=False,
            action=Action.REPLY,
            latency_ms=20,
        ),
        _observation("C-R", 1, error_kind="protocol", latency_ms=30),
        _observation(
            "C-O",
            1,
            intent=Intent.OFF_TOPIC,
            dissatisfied=False,
            action=Action.REPLY,
            latency_ms=40,
        ),
        _observation(
            "C-T",
            1,
            intent=Intent.OTHER,
            dissatisfied=False,
            action=Action.SCHEDULE_FOLLOWUP,
            latency_ms=50,
        ),
    ]

    metrics = compute_metrics(cases, observations, repeats=1, batch_elapsed_seconds=2)

    assert metrics["strict_contract_success_rate"] == pytest.approx(0.8)
    assert metrics["canonical_intent_accuracy"] == pytest.approx(0.6)
    assert metrics["accepted_intent_accuracy"] == pytest.approx(0.8)
    assert metrics["accepted_action_accuracy"] == pytest.approx(0.8)
    assert metrics["joint_accepted_accuracy"] == pytest.approx(0.6)
    assert metrics["dissatisfaction"]["accuracy"] == pytest.approx(0.6)
    assert metrics["dissatisfaction"]["errors"] == 1
    assert metrics["intent_confusion"]["rejected"][ERROR_LABEL] == 1
    assert metrics["action_confusion"]["mark_not_interested"][ERROR_LABEL] == 1
    assert metrics["latency_ms"]["all_p50"] == 30
    assert metrics["latency_ms"]["all_p95"] == 50
    assert metrics["error_case_ids"] == ["C-R"]


def test_consistency_counts_disagreement_and_any_error_as_non_unanimous() -> None:
    cases = [_case("SAME", Intent.INTERESTED), _case("DIFF", Intent.OTHER)]
    observations = [
        *[
            _observation(
                "SAME",
                repeat,
                intent=Intent.INTERESTED,
                dissatisfied=False,
                action=Action.REPLY,
            )
            for repeat in (1, 2, 3)
        ],
        _observation(
            "DIFF",
            1,
            intent=Intent.OTHER,
            dissatisfied=False,
            action=Action.REPLY,
        ),
        _observation(
            "DIFF",
            2,
            intent=Intent.OTHER,
            dissatisfied=False,
            action=Action.REPLY,
        ),
        _observation("DIFF", 3, error_kind="unavailable"),
    ]

    metrics = compute_metrics(cases, observations, repeats=3, batch_elapsed_seconds=1)

    assert metrics["consistency"]["unanimous_rate"] == pytest.approx(0.5)
    assert metrics["consistency"]["mean_majority_share"] == pytest.approx(5 / 6)
    assert metrics["consistency"]["disagreement_case_ids"] == ["DIFF"]


def test_all_errors_have_zero_majority_and_negative_error_is_not_false_positive() -> None:
    cases = [_case("FAIL", Intent.OTHER)]
    observations = [_observation("FAIL", repeat, error_kind="unavailable") for repeat in (1, 2, 3)]

    metrics = compute_metrics(cases, observations, repeats=3, batch_elapsed_seconds=1)

    assert metrics["dissatisfaction"]["errors"] == 3
    assert metrics["dissatisfaction"]["fp"] == 0
    assert metrics["dissatisfaction"]["accuracy"] == 0
    assert metrics["consistency"]["unanimous_rate"] == 0
    assert metrics["consistency"]["mean_majority_share"] == 0


def test_observation_requires_error_xor_complete_predictions() -> None:
    with pytest.raises(ValidationError, match="cannot contain predictions"):
        _observation(
            "BAD",
            1,
            intent=Intent.INTERESTED,
            dissatisfied=False,
            action=Action.REPLY,
            error_kind="protocol",
        )
    with pytest.raises(ValidationError, match="require all predictions"):
        _observation("BAD", 1)


def test_gemini_report_uses_gemini_endpoint_and_reproduction_variables(
    tmp_path: Path,
) -> None:
    cases = [_case("GEMINI", Intent.INTERESTED)]
    observations = [
        _observation(
            "GEMINI",
            1,
            intent=Intent.INTERESTED,
            dissatisfied=False,
            action=Action.REPLY,
        )
    ]
    settings = Settings(
        llm_provider="gemini",
        gemini_api_key=SecretStr("unit-gemini-key"),
        gemini_model="unit-gemini-model",
    )
    metrics = compute_metrics(cases, observations, repeats=1, batch_elapsed_seconds=1)

    report = render_markdown_report(
        settings=settings,
        dataset_path=tmp_path / "cases.jsonl",
        cases=cases,
        metrics=metrics,
        repeats=1,
        concurrency=1,
    )
    payload = sanitized_results_payload(
        settings=settings,
        dataset_path=tmp_path / "cases.jsonl",
        observations=observations,
        metrics=metrics,
        repeats=1,
        concurrency=1,
    )

    assert "generativelanguage.googleapis.com" in report
    assert "GEMINI_API_KEY" in report
    assert "GEMINI_MODEL=unit-gemini-model" in report
    assert "api.openlux.ai" not in report
    assert "LLM_API_KEY" not in report
    assert payload["metadata"]["endpoint_host"] == ("generativelanguage.googleapis.com")


class _EvalFakeGateway:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.closed = False

    async def decide(self, customer_message: str, *, history=()):
        del history
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01 if customer_message != "error" else 0.005)
            if customer_message == "error":
                raise RuntimeError("sk-FAKE-PROVIDER-SECRET raw message")
            return ModelDecision(
                intent=Intent.INTERESTED,
                dissatisfied=False,
                action=Action.REPLY,
                reply_draft="SECRET_DRAFT_MUST_NOT_PERSIST",
                rationale="SECRET_RATIONALE_MUST_NOT_PERSIST",
            )
        finally:
            self.active -= 1

    async def review_reply(self, customer_message: str, reply_draft: str) -> LeakageReview:
        del customer_message, reply_draft
        return LeakageReview(safe=True, category="safe", rationale="safe")

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_runner_limits_concurrency_continues_after_error_and_sanitizes(
    tmp_path: Path,
) -> None:
    cases = [
        _case("RUN-A", Intent.INTERESTED, message="normal-a"),
        _case("RUN-B", Intent.INTERESTED, message="error"),
        _case("RUN-C", Intent.INTERESTED, message="normal-c"),
    ]
    gateway = _EvalFakeGateway()
    settings = Settings(
        llm_api_key=SecretStr("sk-FAKE-CONFIG-SECRET"),
        llm_api_base="https://provider.test/v1",
        llm_model="unit-model",
    )

    observations, elapsed = await run_evaluation(
        settings=settings,
        cases=cases,
        repeats=2,
        concurrency=2,
        progress=False,
        gateway=gateway,
    )
    metrics = compute_metrics(cases, observations, repeats=2, batch_elapsed_seconds=elapsed)
    report = render_markdown_report(
        settings=settings,
        dataset_path=tmp_path / "cases.jsonl",
        cases=cases,
        metrics=metrics,
        repeats=2,
        concurrency=2,
    )
    payload = sanitized_results_payload(
        settings=settings,
        dataset_path=tmp_path / "cases.jsonl",
        observations=observations,
        metrics=metrics,
        repeats=2,
        concurrency=2,
    )
    serialized = json.dumps(payload, ensure_ascii=False)

    assert gateway.max_active <= 2
    assert gateway.closed is True
    assert [(item.case_id, item.repeat) for item in observations] == sorted(
        (item.case_id, item.repeat) for item in observations
    )
    assert sum(item.error_kind == "unexpected:RuntimeError" for item in observations) == 2
    for forbidden in (
        "sk-FAKE",
        "SECRET_DRAFT",
        "SECRET_RATIONALE",
        "normal-a",
        "raw message",
    ):
        assert forbidden not in report
        assert forbidden not in serialized
