from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Intent(StrEnum):
    INTERESTED = "interested"
    NEEDS_MORE_INFO = "needs_more_info"
    REJECTED = "rejected"
    OFF_TOPIC = "off_topic"
    OTHER = "other"


class Action(StrEnum):
    """The complete action capability exposed to the model."""

    REPLY = "reply"
    SCHEDULE_FOLLOWUP = "schedule_followup"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    MARK_NOT_INTERESTED = "mark_not_interested"


class Lifecycle(StrEnum):
    ACTIVE = "active"
    HUMAN_CONTROLLED = "human_controlled"
    NOT_INTERESTED = "not_interested"


class TurnOutcome(StrEnum):
    SENT = "sent"
    SCHEDULED = "scheduled"
    ESCALATED = "escalated"
    CLOSED = "closed"
    SILENT = "silent"
    RATE_LIMITED = "rate_limited"
    SAFETY_BLOCKED = "safety_blocked"
    MODEL_ERROR = "model_error"


class GuardStatus(StrEnum):
    PASSED = "passed"
    ENFORCED = "enforced"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class Sender(StrEnum):
    CUSTOMER = "customer"
    AGENT = "agent"
    SYSTEM = "system"


class ModelDecision(BaseModel):
    """Strict model contract. Extra fields and unknown actions are rejected."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    intent: Intent = Field(description="The customer's primary intent")
    dissatisfied: bool = Field(
        description="Whether the customer is clearly dissatisfied; independent of intent"
    )
    action: Action = Field(description="Exactly one action from the server allowlist")
    reply_draft: Annotated[str | None, Field(max_length=600)] = Field(
        default=None,
        description="A short customer-facing draft, required only for the reply action",
    )
    rationale: Annotated[str, Field(min_length=1, max_length=240)] = Field(
        description="A short, non-sensitive explanation for the operator audit view"
    )

    @model_validator(mode="after")
    def validate_reply_contract(self) -> ModelDecision:
        if self.action is Action.REPLY and not self.reply_draft:
            raise ValueError("reply_draft is required when action is reply")
        if self.action is not Action.REPLY and self.reply_draft is not None:
            raise ValueError("reply_draft must be null when action is not reply")
        if self.action is Action.MARK_NOT_INTERESTED and self.intent is not Intent.REJECTED:
            raise ValueError("mark_not_interested requires rejected intent")
        if self.intent is Intent.REJECTED and self.action is not Action.MARK_NOT_INTERESTED:
            raise ValueError("rejected intent requires mark_not_interested")
        return self


class LeakageReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    safe: bool
    category: Annotated[str, Field(min_length=1, max_length=80)]
    rationale: Annotated[str, Field(min_length=1, max_length=180)]


class GuardEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    status: GuardStatus
    reason: str


@dataclass(frozen=True, slots=True)
class Conversation:
    id: str
    name: str
    lifecycle: Lifecycle
    strike_count: int
    followup_pending: bool
    activation_epoch: int
    last_outbound_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Enforcement:
    action: Action
    lifecycle: Lifecycle
    strike_count: int
    followup_pending: bool
    activation_epoch: int
    overridden: bool
    override_reason: str | None


def enforce_state_machine(state: Conversation, decision: ModelDecision) -> Enforcement:
    """Apply deterministic business policy to a validated model suggestion.

    The model never controls lifecycle or the shared strike counter directly.
    """

    if state.lifecycle is not Lifecycle.ACTIVE:
        raise ValueError("inactive conversations cannot accept model decisions")

    is_strike = decision.intent is Intent.OFF_TOPIC or decision.dissatisfied
    strike_count = min(2, state.strike_count + 1) if is_strike else 0

    if strike_count >= 2:
        return Enforcement(
            action=Action.ESCALATE_TO_HUMAN,
            lifecycle=Lifecycle.HUMAN_CONTROLLED,
            strike_count=2,
            followup_pending=False,
            activation_epoch=state.activation_epoch + 1,
            overridden=decision.action is not Action.ESCALATE_TO_HUMAN,
            override_reason="连续两轮答非所问或明显不满，确定性状态机强制转人工",
        )

    if decision.action is Action.ESCALATE_TO_HUMAN:
        lifecycle = Lifecycle.HUMAN_CONTROLLED
    elif decision.action is Action.MARK_NOT_INTERESTED:
        lifecycle = Lifecycle.NOT_INTERESTED
    else:
        lifecycle = Lifecycle.ACTIVE

    if decision.action is Action.SCHEDULE_FOLLOWUP:
        followup_pending = True
    elif lifecycle is Lifecycle.ACTIVE:
        # A reply only clears this durable fallback after it is actually sent.
        followup_pending = state.followup_pending
    else:
        followup_pending = False

    return Enforcement(
        action=decision.action,
        lifecycle=lifecycle,
        strike_count=strike_count,
        followup_pending=followup_pending,
        activation_epoch=(
            state.activation_epoch + 1
            if lifecycle is not Lifecycle.ACTIVE
            else state.activation_epoch
        ),
        overridden=False,
        override_reason=None,
    )


class ClassificationView(BaseModel):
    intent: Intent
    dissatisfied: bool


class TurnResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    conversation_id: str
    classification: ClassificationView | None = None
    model_action: Action | None = None
    enforced_action: Action | None = None
    outcome: TurnOutcome
    strike_count: int = Field(ge=0, le=2)
    lifecycle: Lifecycle
    next_allowed_at: datetime | None = None
    final_reply: str | None = None
    rationale: str | None = None
    guard_events: list[GuardEvent]
