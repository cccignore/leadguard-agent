from __future__ import annotations

import os

import pytest

from leadguard.config import Settings
from leadguard.domain import Action, Intent
from leadguard.llm import build_llm_gateway

_settings = Settings()

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_LLM") != "1" or not _settings.llm_configured,
        reason="set RUN_LIVE_LLM=1 and configure the selected provider",
    ),
]


@pytest.mark.asyncio
async def test_real_provider_returns_strict_decision_and_reviews_reply() -> None:
    """Opt-in evidence that production classification traverses a real LLM."""

    gateway = build_llm_gateway(_settings)
    try:
        decision = await gateway.decide("我对你们的服务有兴趣，请先简单介绍一下公开产品能力。")
        assert decision.intent is Intent.INTERESTED
        assert decision.dissatisfied is False
        assert decision.action is Action.REPLY
        assert decision.reply_draft

        review = await gateway.review_reply(
            "我对你们的服务有兴趣，请先简单介绍一下公开产品能力。",
            "您好，我们可协助企业进行客户沟通和销售线索初筛，具体方案由顾问确认。",
        )
        assert review.safe is True
    finally:
        await gateway.aclose()
