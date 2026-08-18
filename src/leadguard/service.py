from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from leadguard.clock import Clock
from leadguard.domain import (
    Action,
    ClassificationView,
    Conversation,
    GuardEvent,
    GuardStatus,
    Lifecycle,
    TurnOutcome,
    TurnResult,
)
from leadguard.llm import DialogueTurn, LLMGateway
from leadguard.output_guard import OutputGuard
from leadguard.storage import DuplicateRequestInProgressError, SQLiteStore


@dataclass(frozen=True, slots=True)
class TurnContext:
    request_id: str
    conversation_id: str
    classification: ClassificationView
    model_action: Action
    strike_count: int
    rationale: str


class AgentService:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        llm: LLMGateway,
        clock: Clock,
        output_guard: OutputGuard,
    ) -> None:
        self.store = store
        self.llm = llm
        self.clock = clock
        self.output_guard = output_guard
        self._conversation_locks: dict[str, asyncio.Lock] = {}

    async def process_message(
        self,
        conversation_id: str,
        request_id: str,
        content: str,
    ) -> TurnResult:
        # The supported demo runtime uses one ASGI process. Serializing from before
        # the inbound write makes the persisted message order the same order in
        # which the shared strike state is consumed, regardless of LLM latency.
        lock = self._conversation_locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            return await self._process_message_serialized(conversation_id, request_id, content)

    async def _process_message_serialized(
        self,
        conversation_id: str,
        request_id: str,
        content: str,
    ) -> TurnResult:
        cached = self.store.get_turn_result(conversation_id, request_id)
        if cached is not None:
            return cached

        started_at = self.clock.now()
        try:
            starting_state = self.store.record_inbound(
                conversation_id, request_id, content, started_at
            )
        except DuplicateRequestInProgressError:
            cached = self.store.get_turn_result(conversation_id, request_id)
            if cached is not None:
                return cached
            raise

        if starting_state.lifecycle is not Lifecycle.ACTIVE:
            return self._finish_inactive_turn(starting_state, request_id, now=started_at)

        trace = [
            GuardEvent(
                stage="state_precheck",
                status=GuardStatus.PASSED,
                reason="会话为 active，允许调用 LLM 进行分类",
            )
        ]
        history = self._recent_public_dialogue(conversation_id, request_id)
        try:
            decision = await self.llm.decide(content, history=history)
        except Exception:
            return self._fail_closed_for_model_error(
                starting_state, request_id, trace, now=self.clock.now()
            )

        trace.extend(
            [
                GuardEvent(
                    stage="llm_classification",
                    status=GuardStatus.PASSED,
                    reason="LLM 返回的意图、情绪与动作已通过严格 schema 校验",
                ),
                GuardEvent(
                    stage="action_allowlist",
                    status=GuardStatus.PASSED,
                    reason="模型动作属于服务端 Action 枚举；未向模型注册任何工具",
                ),
            ]
        )

        enforcement = self.store.apply_decision(
            conversation_id,
            decision,
            expected_activation_epoch=starting_state.activation_epoch,
            now=self.clock.now(),
        )
        if enforcement is None:
            current = self.store.get_conversation(conversation_id)
            trace.append(
                GuardEvent(
                    stage="state_machine",
                    status=GuardStatus.BLOCKED,
                    reason="LLM 在途期间状态或激活代次已变化，丢弃迟到结果",
                )
            )
            return self._finish_silent(
                current,
                request_id,
                trace,
                now=self.clock.now(),
                classification=ClassificationView(
                    intent=decision.intent, dissatisfied=decision.dissatisfied
                ),
                model_action=decision.action,
            )

        trace.append(
            GuardEvent(
                stage="state_machine",
                status=(GuardStatus.ENFORCED if enforcement.overridden else GuardStatus.PASSED),
                reason=(enforcement.override_reason or "共享异常计数器与生命周期规则已由代码应用"),
            )
        )

        context = TurnContext(
            request_id=request_id,
            conversation_id=conversation_id,
            classification=ClassificationView(
                intent=decision.intent,
                dissatisfied=decision.dissatisfied,
            ),
            model_action=decision.action,
            strike_count=enforcement.strike_count,
            rationale="模型只提供结构化建议；下列实际动作由服务端策略裁决。",
        )

        if enforcement.action is Action.ESCALATE_TO_HUMAN:
            trace.extend(_skipped_reply_guards("转人工动作不生成或发送回复"))
            trace.append(
                GuardEvent(
                    stage="execute",
                    status=GuardStatus.ENFORCED,
                    reason="会话已进入 human_controlled；后续客户消息严格静默",
                )
            )
            self.store.append_system_message(
                conversation_id,
                "已转交人工跟进。Agent 将保持静默，直到人工操作员重新激活。",
                self.clock.now(),
            )
            return self._save(
                _make_result(
                    context,
                    enforced_action=Action.ESCALATE_TO_HUMAN,
                    outcome=TurnOutcome.ESCALATED,
                    lifecycle=Lifecycle.HUMAN_CONTROLLED,
                    guard_events=trace,
                )
            )

        if enforcement.action is Action.MARK_NOT_INTERESTED:
            trace.extend(_skipped_reply_guards("结束会话动作不生成或发送回复"))
            trace.append(
                GuardEvent(
                    stage="execute",
                    status=GuardStatus.ENFORCED,
                    reason="会话已标记为不感兴趣并终止自动处理",
                )
            )
            self.store.append_system_message(
                conversation_id,
                "客户已标记为不感兴趣，会话结束。",
                self.clock.now(),
            )
            return self._save(
                _make_result(
                    context,
                    enforced_action=Action.MARK_NOT_INTERESTED,
                    outcome=TurnOutcome.CLOSED,
                    lifecycle=Lifecycle.NOT_INTERESTED,
                    guard_events=trace,
                )
            )

        if enforcement.action is Action.SCHEDULE_FOLLOWUP:
            trace.extend(_skipped_reply_guards("稍后跟进动作本轮不发送回复"))
            trace.append(
                GuardEvent(
                    stage="execute",
                    status=GuardStatus.ENFORCED,
                    reason="已持久化稍后跟进标记，本轮无外发消息",
                )
            )
            self.store.append_system_message(
                conversation_id,
                "已标记稍后跟进，本轮未自动回复。",
                self.clock.now(),
            )
            return self._save(
                _make_result(
                    context,
                    enforced_action=Action.SCHEDULE_FOLLOWUP,
                    outcome=TurnOutcome.SCHEDULED,
                    lifecycle=Lifecycle.ACTIVE,
                    guard_events=trace,
                )
            )

        if enforcement.action is not Action.REPLY:  # Defensive exhaustiveness guard.
            raise AssertionError(f"unhandled allowlisted action: {enforcement.action}")

        assert decision.reply_draft is not None
        guard_result = await self.output_guard.inspect(content, decision.reply_draft)
        if not guard_result.safe:
            return self._fail_closed_for_unsafe_draft(
                starting_state=starting_state,
                request_id=request_id,
                trace=trace,
                context=context,
                reason=guard_result.reason,
                expected_activation_epoch=enforcement.activation_epoch,
                now=self.clock.now(),
            )

        trace.append(
            GuardEvent(
                stage="leakage_guard",
                status=GuardStatus.PASSED,
                reason=guard_result.reason,
            )
        )
        send = self.store.send_reply(
            conversation_id,
            decision.reply_draft,
            expected_activation_epoch=enforcement.activation_epoch,
            now=self.clock.now(),
        )
        if send.sent:
            trace.extend(
                [
                    GuardEvent(
                        stage="rate_limit",
                        status=GuardStatus.PASSED,
                        reason="原子滑动窗口检查通过，距上次真实发送已满 60 秒或无历史发送",
                    ),
                    GuardEvent(
                        stage="execute",
                        status=GuardStatus.ENFORCED,
                        reason="回复仅通过统一 Outbound Gateway 写入模拟发送通道",
                    ),
                ]
            )
            return self._save(
                _make_result(
                    context,
                    enforced_action=Action.REPLY,
                    outcome=TurnOutcome.SENT,
                    lifecycle=Lifecycle.ACTIVE,
                    final_reply=decision.reply_draft,
                    guard_events=trace,
                )
            )

        current = self.store.get_conversation(conversation_id)
        if send.next_allowed_at is not None:
            trace.extend(
                [
                    GuardEvent(
                        stage="rate_limit",
                        status=GuardStatus.BLOCKED,
                        reason="命中同一客户任意 60 秒滑动窗口限制，草稿未发送",
                    ),
                    GuardEvent(
                        stage="execute",
                        status=GuardStatus.SKIPPED,
                        reason="限流发生在最终发送边界，没有 Agent 消息写入",
                    ),
                ]
            )
            self.store.append_system_message(
                conversation_id,
                "回复未发送：命中任意 60 秒滑动窗口限制。",
                self.clock.now(),
            )
            return self._save(
                _make_result(
                    context,
                    enforced_action=Action.REPLY,
                    outcome=TurnOutcome.RATE_LIMITED,
                    lifecycle=current.lifecycle,
                    next_allowed_at=send.next_allowed_at,
                    guard_events=trace,
                )
            )

        trace.append(
            GuardEvent(
                stage="execute",
                status=GuardStatus.BLOCKED,
                reason="发送事务发现状态或激活代次已变化，回复被丢弃",
            )
        )
        return self._finish_silent(
            current,
            request_id,
            trace,
            now=self.clock.now(),
            classification=context.classification,
            model_action=decision.action,
        )

    def _finish_inactive_turn(
        self, state: Conversation, request_id: str, *, now: datetime
    ) -> TurnResult:
        reason = (
            "人工接管中：客户消息已记录，但 Agent 不调用 LLM、不回复、不执行动作。"
            if state.lifecycle is Lifecycle.HUMAN_CONTROLLED
            else "会话已结束：客户消息已记录，但 Agent 不再自动处理。"
        )
        self.store.append_system_message(state.id, reason, now)
        trace = [
            GuardEvent(stage="state_precheck", status=GuardStatus.BLOCKED, reason=reason),
            *_skipped_all_after_precheck(),
        ]
        return self._save(
            TurnResult(
                request_id=request_id,
                conversation_id=state.id,
                outcome=TurnOutcome.SILENT,
                strike_count=state.strike_count,
                lifecycle=state.lifecycle,
                guard_events=trace,
                rationale="终止态预检在任何 LLM 调用之前生效。",
            )
        )

    def _fail_closed_for_model_error(
        self,
        starting_state: Conversation,
        request_id: str,
        trace: list[GuardEvent],
        *,
        now: datetime,
    ) -> TurnResult:
        updated = self.store.force_escalate(starting_state.id, starting_state.activation_epoch, now)
        trace.extend(
            [
                GuardEvent(
                    stage="llm_classification",
                    status=GuardStatus.BLOCKED,
                    reason="LLM 不可用或输出未通过严格 schema；未应用任何部分字段",
                ),
                GuardEvent(
                    stage="state_machine",
                    status=GuardStatus.ENFORCED,
                    reason="失败关闭：转人工且不产生外发消息",
                ),
                *_skipped_reply_guards("模型判断失败，无候选回复可执行"),
            ]
        )
        if updated is None:
            current = self.store.get_conversation(starting_state.id)
            return self._finish_silent(current, request_id, trace, now=now)
        self.store.append_system_message(
            starting_state.id,
            "模型判断失败，已按失败关闭策略转人工；本轮未发送消息。",
            now,
        )
        return self._save(
            TurnResult(
                request_id=request_id,
                conversation_id=starting_state.id,
                enforced_action=Action.ESCALATE_TO_HUMAN,
                outcome=TurnOutcome.ESCALATED,
                strike_count=updated.strike_count,
                lifecycle=updated.lifecycle,
                rationale="模型失败不会降级为关键词分类；服务端直接失败关闭。",
                guard_events=trace,
            )
        )

    def _fail_closed_for_unsafe_draft(
        self,
        *,
        starting_state: Conversation,
        request_id: str,
        trace: list[GuardEvent],
        context: TurnContext,
        reason: str,
        expected_activation_epoch: int,
        now: datetime,
    ) -> TurnResult:
        updated = self.store.force_escalate(starting_state.id, expected_activation_epoch, now)
        trace.extend(
            [
                GuardEvent(
                    stage="leakage_guard",
                    status=GuardStatus.BLOCKED,
                    reason=reason,
                ),
                GuardEvent(
                    stage="rate_limit",
                    status=GuardStatus.SKIPPED,
                    reason="不安全草稿在发送边界之前已被丢弃",
                ),
                GuardEvent(
                    stage="execute",
                    status=GuardStatus.ENFORCED,
                    reason="失败关闭：转人工且不返回、不持久化、不发送候选草稿",
                ),
            ]
        )
        if updated is None:
            current = self.store.get_conversation(starting_state.id)
            return self._finish_silent(
                current,
                request_id,
                trace,
                now=now,
                classification=context.classification,
                model_action=context.model_action,
            )
        self.store.append_system_message(
            starting_state.id,
            "候选回复未通过防泄漏审查，已转人工；危险草稿未保存也未发送。",
            now,
        )
        return self._save(
            _make_result(
                context,
                enforced_action=Action.ESCALATE_TO_HUMAN,
                outcome=TurnOutcome.SAFETY_BLOCKED,
                lifecycle=Lifecycle.HUMAN_CONTROLLED,
                guard_events=trace,
            )
        )

    def _finish_silent(
        self,
        state: Conversation,
        request_id: str,
        trace: list[GuardEvent],
        *,
        now: datetime,
        classification: ClassificationView | None = None,
        model_action: Action | None = None,
    ) -> TurnResult:
        self.store.append_system_message(
            state.id,
            "状态已变化：迟到的模型结果和全部自动动作均被丢弃。",
            now,
        )
        return self._save(
            TurnResult(
                request_id=request_id,
                conversation_id=state.id,
                classification=classification,
                model_action=model_action,
                outcome=TurnOutcome.SILENT,
                strike_count=state.strike_count,
                lifecycle=state.lifecycle,
                guard_events=trace,
                rationale="最终状态复检阻断了过期或越权动作。",
            )
        )

    def _save(self, result: TurnResult) -> TurnResult:
        return self.store.save_turn(result, self.clock.now())

    def _recent_public_dialogue(
        self, conversation_id: str, current_request_id: str
    ) -> tuple[DialogueTurn, ...]:
        candidates: list[DialogueTurn] = []
        for message in self.store.list_messages(conversation_id):
            if message["request_id"] == current_request_id:
                continue
            sender = message["sender"]
            if sender not in {"customer", "agent"}:
                continue
            role: Literal["customer", "agent"] = "customer" if sender == "customer" else "agent"
            candidates.append(DialogueTurn(role=role, content=message["content"][:500]))

        selected: list[DialogueTurn] = []
        remaining_chars = 2_400
        for turn in reversed(candidates):
            if len(selected) >= 6 or remaining_chars <= 0:
                break
            content = turn.content[:remaining_chars]
            selected.append(DialogueTurn(role=turn.role, content=content))
            remaining_chars -= len(content)
        return tuple(reversed(selected))


def _skipped_reply_guards(reason: str) -> list[GuardEvent]:
    return [
        GuardEvent(stage="leakage_guard", status=GuardStatus.SKIPPED, reason=reason),
        GuardEvent(stage="rate_limit", status=GuardStatus.SKIPPED, reason=reason),
    ]


def _skipped_all_after_precheck() -> list[GuardEvent]:
    reason = "终止态在入口即阻断，后续阶段未运行"
    return [
        GuardEvent(stage="llm_classification", status=GuardStatus.SKIPPED, reason=reason),
        GuardEvent(stage="state_machine", status=GuardStatus.SKIPPED, reason=reason),
        GuardEvent(stage="action_allowlist", status=GuardStatus.SKIPPED, reason=reason),
        GuardEvent(stage="leakage_guard", status=GuardStatus.SKIPPED, reason=reason),
        GuardEvent(stage="rate_limit", status=GuardStatus.SKIPPED, reason=reason),
        GuardEvent(stage="execute", status=GuardStatus.SKIPPED, reason=reason),
    ]


def _make_result(
    context: TurnContext,
    *,
    enforced_action: Action,
    outcome: TurnOutcome,
    lifecycle: Lifecycle,
    guard_events: list[GuardEvent],
    next_allowed_at: datetime | None = None,
    final_reply: str | None = None,
) -> TurnResult:
    return TurnResult(
        request_id=context.request_id,
        conversation_id=context.conversation_id,
        classification=context.classification,
        model_action=context.model_action,
        enforced_action=enforced_action,
        outcome=outcome,
        strike_count=context.strike_count,
        lifecycle=lifecycle,
        next_allowed_at=next_allowed_at,
        final_reply=final_reply,
        rationale=context.rationale,
        guard_events=guard_events,
    )
