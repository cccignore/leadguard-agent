from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from leadguard.clock import Clock, SystemClock
from leadguard.config import Settings
from leadguard.domain import Conversation, Lifecycle, TurnResult
from leadguard.llm import LLMGateway, UnavailableGateway, build_llm_gateway
from leadguard.output_guard import OutputGuard
from leadguard.service import AgentService
from leadguard.storage import (
    ConversationNotFoundError,
    DuplicateRequestInProgressError,
    InvalidTransitionError,
    SQLiteStore,
)


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=60)


class CustomerMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    content: str = Field(min_length=1, max_length=2_000)
    request_id: UUID


class ConversationView(BaseModel):
    id: str
    name: str
    lifecycle: Lifecycle
    strike_count: int
    followup_pending: bool
    last_outbound_at: str | None
    next_allowed_at: str | None
    created_at: str
    updated_at: str


class MessageView(BaseModel):
    id: int
    sender: str
    content: str
    request_id: str | None
    created_at: str


class ConversationDetail(BaseModel):
    conversation: ConversationView
    messages: list[MessageView]
    turns: list[TurnResult]


def create_app(
    settings: Settings | None = None,
    *,
    llm: LLMGateway | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    runtime_settings = settings or Settings()
    runtime_clock = clock or SystemClock()
    store = SQLiteStore(
        runtime_settings.database_path,
        rate_limit_seconds=runtime_settings.rate_limit_seconds,
    )
    provided_llm = llm

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store.initialize()
        if not store.list_conversations():
            store.create_conversation("演示客户", runtime_clock.now())

        gateway: LLMGateway
        if provided_llm is not None:
            gateway = provided_llm
        elif runtime_settings.llm_configured:
            gateway = build_llm_gateway(runtime_settings)
        else:
            gateway = UnavailableGateway()

        active_key = runtime_settings.active_api_key

        app.state.settings = runtime_settings
        app.state.store = store
        app.state.llm = gateway
        app.state.llm_configured = not isinstance(gateway, UnavailableGateway)
        app.state.service = AgentService(
            store=store,
            llm=gateway,
            clock=runtime_clock,
            output_guard=OutputGuard(
                gateway,
                runtime_settings.max_reply_chars,
                protected_values=((active_key.get_secret_value(),) if active_key else ()),
            ),
        )
        try:
            yield
        finally:
            await gateway.aclose()

    app = FastAPI(
        title=runtime_settings.app_name,
        version="1.0.0",
        description=(
            "LLM-based lead qualification with code-enforced state, action, "
            "rate-limit and outbound safety boundaries."
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(ConversationNotFoundError)
    async def conversation_not_found(
        request: Request, error: ConversationNotFoundError
    ) -> JSONResponse:
        del request, error
        return JSONResponse(status_code=404, content={"detail": "conversation not found"})

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, object]:
        configured = runtime_settings.llm_configured or provided_llm is not None
        gateway_status = (
            "verified"
            if provided_llm is not None
            else getattr(request.app.state.llm, "credential_status", "not_checked")
        )
        if not configured:
            gateway_status = "missing"
        health_status = (
            "ready"
            if gateway_status == "verified"
            else "error"
            if gateway_status == "error"
            else "configured"
        )
        if not configured:
            health_status = "needs_configuration"
        return {
            "status": health_status,
            "llm_configured": configured,
            "credential_status": gateway_status,
            "provider": (
                runtime_settings.llm_provider
                if configured and provided_llm is None
                else "injected"
                if configured
                else None
            ),
            "model": runtime_settings.active_model if configured else None,
            "rate_limit_seconds": runtime_settings.rate_limit_seconds,
            "version": request.app.version,
        }

    @app.get("/api/conversations", response_model=list[ConversationView])
    async def list_conversations(request: Request) -> list[ConversationView]:
        return [
            _conversation_view(item, runtime_settings.rate_limit_seconds)
            for item in request.app.state.store.list_conversations()
        ]

    @app.post(
        "/api/conversations",
        response_model=ConversationView,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_conversation(
        payload: CreateConversationRequest, request: Request
    ) -> ConversationView:
        item = request.app.state.store.create_conversation(payload.name, runtime_clock.now())
        return _conversation_view(item, runtime_settings.rate_limit_seconds)

    @app.get("/api/conversations/{conversation_id}", response_model=ConversationDetail)
    async def conversation_detail(conversation_id: str, request: Request) -> ConversationDetail:
        current_store: SQLiteStore = request.app.state.store
        conversation = current_store.get_conversation(conversation_id)
        messages = [
            MessageView(
                id=item["id"],
                sender=item["sender"],
                content=item["content"],
                request_id=item["request_id"],
                created_at=_iso(item["created_at"]),
            )
            for item in current_store.list_messages(conversation_id)
        ]
        return ConversationDetail(
            conversation=_conversation_view(conversation, runtime_settings.rate_limit_seconds),
            messages=messages,
            turns=current_store.list_turns(conversation_id),
        )

    @app.post(
        "/api/conversations/{conversation_id}/messages",
        response_model=TurnResult,
    )
    async def customer_message(
        conversation_id: str,
        payload: CustomerMessageRequest,
        request: Request,
    ) -> TurnResult:
        if len(payload.content) > runtime_settings.max_customer_message_chars:
            raise HTTPException(
                status_code=422,
                detail=(
                    "message exceeds configured limit of "
                    f"{runtime_settings.max_customer_message_chars} characters"
                ),
            )
        service: AgentService = request.app.state.service
        if not request.app.state.llm_configured:
            raise HTTPException(
                status_code=503,
                detail=("the selected LLM provider is not configured; no keyword fallback is used"),
            )
        try:
            return await service.process_message(
                conversation_id,
                str(payload.request_id),
                payload.content,
            )
        except DuplicateRequestInProgressError as error:
            raise HTTPException(
                status_code=409,
                detail="the same request_id is already being processed",
            ) from error

    @app.post(
        "/api/conversations/{conversation_id}/reactivate",
        response_model=ConversationView,
    )
    async def reactivate(conversation_id: str, request: Request) -> ConversationView:
        try:
            item = request.app.state.store.reactivate(conversation_id, runtime_clock.now())
        except InvalidTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _conversation_view(item, runtime_settings.rate_limit_seconds)

    @app.post("/api/demo/reset", response_model=list[ConversationView])
    async def reset_demo(request: Request) -> list[ConversationView]:
        current_store: SQLiteStore = request.app.state.store
        current_store.clear()
        for name in ("陈先生", "李女士", "Eve · 攻击演示"):
            current_store.create_conversation(name, runtime_clock.now())
        return [
            _conversation_view(item, runtime_settings.rate_limit_seconds)
            for item in current_store.list_conversations()
        ]

    web_directory = Path(__file__).resolve().parents[2] / "web"
    if web_directory.is_dir():
        app.mount("/", StaticFiles(directory=web_directory, html=True), name="web")

    return app


def _conversation_view(item: Conversation, rate_limit_seconds: int) -> ConversationView:
    next_allowed = (
        item.last_outbound_at + timedelta(seconds=rate_limit_seconds)
        if item.last_outbound_at is not None
        else None
    )
    return ConversationView(
        id=item.id,
        name=item.name,
        lifecycle=item.lifecycle,
        strike_count=item.strike_count,
        followup_pending=item.followup_pending,
        last_outbound_at=_optional_iso(item.last_outbound_at),
        next_allowed_at=_optional_iso(next_allowed),
        created_at=_iso(item.created_at),
        updated_at=_iso(item.updated_at),
    )


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _optional_iso(value: datetime | None) -> str | None:
    return _iso(value) if value is not None else None


app = create_app()
