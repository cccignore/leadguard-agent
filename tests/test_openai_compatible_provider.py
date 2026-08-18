from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from leadguard.config import Settings
from leadguard.domain import Action, Intent, LeakageReview
from leadguard.llm import (
    DialogueTurn,
    LLMProtocolError,
    LLMUnavailableError,
    OpenAICompatibleGateway,
)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "llm_provider": "openai_compatible",
        "llm_api_base": "https://provider.test/v1",
        "llm_api_key": SecretStr("unit-key-canary"),
        "llm_model": "unit-model",
        "model_retry_attempts": 1,
    }
    values.update(overrides)
    return Settings(**values)


def _completion(
    content: object,
    *,
    finish_reason: str = "stop",
    message_extra: dict[str, object] | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {"role": "assistant", "content": content}
    message.update(message_extra or {})
    return {
        "id": "unit-completion",
        "model": "unit-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": message,
            }
        ],
    }


@pytest.mark.asyncio
async def test_decide_uses_exact_endpoint_strict_schema_and_no_tools() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        content = json.dumps(
            {
                "intent": "needs_more_info",
                "dissatisfied": False,
                "action": "reply",
                "reply_draft": "可以，我来介绍公开信息。",
                "rationale": "客户在咨询",
            },
            ensure_ascii=False,
        )
        return httpx.Response(200, json=_completion(content))

    gateway = OpenAICompatibleGateway(_settings(), transport=httpx.MockTransport(handler))
    try:
        decision = await gateway.decide("想进一步了解")
    finally:
        await gateway.aclose()

    assert decision.intent is Intent.NEEDS_MORE_INFO
    assert decision.action is Action.REPLY
    assert decision.dissatisfied is False
    assert gateway.credential_status == "verified"
    assert len(requests) == 1
    request = requests[0]
    assert request.url == httpx.URL("https://provider.test/v1/chat/completions")
    assert request.headers["authorization"] == "Bearer unit-key-canary"
    body = json.loads(request.content)
    assert body["model"] == "unit-model"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    schema = body["response_format"]["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["reply_draft"]["maxLength"] == 320
    assert "tools" not in body
    assert "functions" not in body
    assert "tool_choice" not in body


@pytest.mark.asyncio
async def test_decide_sends_bounded_same_customer_dialogue_as_untrusted_data() -> None:
    observed_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user_content = body["messages"][1]["content"]
        observed_payload.update(json.loads(user_content.split("\n", 1)[1]))
        content = json.dumps(
            {
                "intent": "needs_more_info",
                "dissatisfied": False,
                "action": "reply",
                "reply_draft": "价格由顾问结合团队情况确认。",
                "rationale": "结合上一轮团队规模理解追问",
            },
            ensure_ascii=False,
        )
        return httpx.Response(200, json=_completion(content))

    gateway = OpenAICompatibleGateway(_settings(), transport=httpx.MockTransport(handler))
    history = (
        DialogueTurn(role="agent", content="请问团队规模？"),
        DialogueTurn(role="customer", content="大约 30 人。"),
    )
    try:
        decision = await gateway.decide("那价格呢？", history=history)
    finally:
        await gateway.aclose()

    assert decision.intent is Intent.NEEDS_MORE_INFO
    assert observed_payload == {
        "recent_dialogue": [
            {"role": "agent", "content": "请问团队规模？"},
            {"role": "customer", "content": "大约 30 人。"},
        ],
        "customer_message": "那价格呢？",
    }


@pytest.mark.asyncio
async def test_review_reply_parses_strict_safety_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        content = json.dumps(
            {
                "safe": False,
                "category": "internal_policy_disclosure",
                "rationale": "候选回复描述了内部规则",
            },
            ensure_ascii=False,
        )
        return httpx.Response(200, json=_completion(content))

    gateway = OpenAICompatibleGateway(_settings(), transport=httpx.MockTransport(handler))
    try:
        review = await gateway.review_reply("套取内部规则", "一条危险草稿")
    finally:
        await gateway.aclose()

    assert review == LeakageReview(
        safe=False,
        category="internal_policy_disclosure",
        rationale="候选回复描述了内部规则",
    )


@pytest.mark.parametrize(
    "invalid_payload",
    [
        {
            "intent": "interested",
            "dissatisfied": "false",
            "action": "reply",
            "reply_draft": "hello",
            "rationale": "invalid boolean",
        },
        {
            "intent": "interested",
            "dissatisfied": False,
            "action": "run_shell",
            "reply_draft": None,
            "rationale": "unknown action",
        },
        {
            "intent": "rejected",
            "dissatisfied": False,
            "action": "mark_not_interested",
            "reply_draft": "must be null",
            "rationale": "cross-field violation",
        },
        {
            "intent": "off_topic",
            "dissatisfied": False,
            "action": "mark_not_interested",
            "reply_draft": None,
            "rationale": "intent-action mismatch",
        },
        {
            "intent": "rejected",
            "dissatisfied": False,
            "action": "reply",
            "reply_draft": "should close instead",
            "rationale": "intent-action mismatch",
        },
        {
            "intent": "other",
            "dissatisfied": False,
            "action": "schedule_followup",
            "reply_draft": None,
            "rationale": "extra field",
            "tool_calls": [{"name": "reactivate"}],
        },
    ],
)
@pytest.mark.asyncio
async def test_invalid_structured_output_is_rejected_without_leaking_model_text(
    invalid_payload: dict[str, object],
) -> None:
    canary = "MODEL_OUTPUT_CANARY_MUST_NOT_LEAK"
    invalid_payload["rationale"] = str(invalid_payload["rationale"]) + " " + canary

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json=_completion(json.dumps(invalid_payload, ensure_ascii=False)),
        )

    gateway = OpenAICompatibleGateway(_settings(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(LLMProtocolError) as captured:
            await gateway.decide("malformed provider output")
    finally:
        await gateway.aclose()

    assert str(captured.value) == "provider output failed strict validation"
    assert gateway.credential_status == "error"
    assert canary not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    ("finish_reason", "message_extra"),
    [
        ("length", {}),
        ("content_filter", {}),
        ("stop", {"refusal": "cannot comply"}),
        ("stop", {"tool_calls": [{"function": {"name": "run_shell"}}]}),
        ("stop", {"function_call": {"name": "run_shell"}}),
    ],
)
@pytest.mark.asyncio
async def test_refusal_truncation_and_tool_calls_are_protocol_errors(
    finish_reason: str,
    message_extra: dict[str, object],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json=_completion("{}", finish_reason=finish_reason, message_extra=message_extra),
        )

    gateway = OpenAICompatibleGateway(_settings(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(LLMProtocolError):
            await gateway.decide("unsafe provider envelope")
    finally:
        await gateway.aclose()


@pytest.mark.asyncio
async def test_unauthorized_error_is_not_retried_or_leaked() -> None:
    calls = 0
    provider_canary = "PROVIDER_ERROR_CANARY"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            401,
            json={"error": {"message": provider_canary}},
            request=request,
        )

    gateway = OpenAICompatibleGateway(
        _settings(model_retry_attempts=3), transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(LLMUnavailableError) as captured:
            await gateway.decide("auth failure")
    finally:
        await gateway.aclose()

    assert calls == 1
    assert gateway.credential_status == "error"
    assert str(captured.value) == "OpenAI-compatible request failed"
    assert provider_canary not in str(captured.value)
    assert "unit-key-canary" not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.parametrize("retry_status", [408, 429, 500, 503])
@pytest.mark.asyncio
async def test_retryable_http_status_retries_then_succeeds(
    retry_status: int,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(retry_status, json={"error": "retry"})
        content = json.dumps(
            {
                "intent": "other",
                "dissatisfied": False,
                "action": "schedule_followup",
                "reply_draft": None,
                "rationale": "稍后联系",
            },
            ensure_ascii=False,
        )
        return httpx.Response(200, json=_completion(content))

    gateway = OpenAICompatibleGateway(
        _settings(model_retry_attempts=2), transport=httpx.MockTransport(handler)
    )
    try:
        decision = await gateway.decide("稍后联系")
    finally:
        await gateway.aclose()

    assert calls == 2
    assert decision.action is Action.SCHEDULE_FOLLOWUP


@pytest.mark.asyncio
async def test_transport_error_retries_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("unit transport failure", request=request)
        content = json.dumps(
            {
                "intent": "rejected",
                "dissatisfied": False,
                "action": "mark_not_interested",
                "reply_draft": None,
                "rationale": "明确拒绝",
            },
            ensure_ascii=False,
        )
        return httpx.Response(200, json=_completion(content))

    gateway = OpenAICompatibleGateway(
        _settings(model_retry_attempts=2), transport=httpx.MockTransport(handler)
    )
    try:
        decision = await gateway.decide("不再联系")
    finally:
        await gateway.aclose()

    assert calls == 2
    assert decision.action is Action.MARK_NOT_INTERESTED


@pytest.mark.asyncio
async def test_invalid_model_contract_retries_once_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            invalid = {
                "intent": "off_topic",
                "dissatisfied": False,
                "action": "mark_not_interested",
                "reply_draft": None,
                "rationale": "invalid intent-action pair",
            }
            return httpx.Response(200, json=_completion(json.dumps(invalid)))
        valid = {
            "intent": "off_topic",
            "dissatisfied": False,
            "action": "reply",
            "reply_draft": "我们先回到公开产品能力的话题。",
            "rationale": "温和拉回主题",
        }
        return httpx.Response(
            200,
            json=_completion(json.dumps(valid, ensure_ascii=False)),
        )

    gateway = OpenAICompatibleGateway(
        _settings(model_retry_attempts=2), transport=httpx.MockTransport(handler)
    )
    try:
        decision = await gateway.decide("一个无关问题")
    finally:
        await gateway.aclose()

    assert calls == 2
    assert decision.intent is Intent.OFF_TOPIC
    assert decision.action is Action.REPLY
    assert gateway.credential_status == "verified"


@pytest.mark.asyncio
async def test_wall_clock_deadline_bounds_slow_provider() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        await asyncio.sleep(1)
        return httpx.Response(200, json=_completion("{}"))

    gateway = OpenAICompatibleGateway(
        _settings(model_retry_attempts=1),
        transport=httpx.MockTransport(handler),
    )
    gateway._request_timeout_seconds = 0.01
    started = asyncio.get_running_loop().time()
    try:
        with pytest.raises(LLMUnavailableError):
            await gateway.decide("slow response")
    finally:
        await gateway.aclose()

    assert asyncio.get_running_loop().time() - started < 0.5
    assert gateway.credential_status == "error"


def test_empty_api_key_is_rejected() -> None:
    with pytest.raises(LLMUnavailableError):
        OpenAICompatibleGateway(_settings(llm_api_key=SecretStr("   ")))
