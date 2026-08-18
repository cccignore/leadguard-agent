from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from leadguard.config import Settings
from leadguard.domain import Action, Intent
from leadguard.llm import (
    DialogueTurn,
    LLMGateway,
    LLMProtocolError,
    LLMUnavailableError,
    build_llm_gateway,
)

INTENT_LABELS = [item.value for item in Intent]
ACTION_LABELS = [item.value for item in Action]
ERROR_LABEL = "ERROR"


class EvalHistoryTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    role: Literal["customer", "agent"]
    content: str = Field(min_length=1, max_length=500)


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Z0-9-]+$")
    language: Literal["zh", "en", "ja", "es", "mixed"]
    message: str = Field(min_length=1, max_length=2_000)
    history: list[EvalHistoryTurn] = Field(default_factory=list, max_length=6)
    canonical_intent: Intent
    accepted_intents: list[Intent] = Field(min_length=1)
    dissatisfied: bool
    accepted_actions: list[Action] = Field(min_length=1)
    tags: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_contract(self) -> EvalCase:
        if self.canonical_intent not in self.accepted_intents:
            raise ValueError("canonical_intent must be accepted")
        if len(set(self.accepted_intents)) != len(self.accepted_intents):
            raise ValueError("accepted_intents must be unique")
        if len(set(self.accepted_actions)) != len(self.accepted_actions):
            raise ValueError("accepted_actions must be unique")
        if len(self.accepted_actions) != 1:
            raise ValueError("exactly one accepted action is required")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("tags must be unique")
        if sum(len(turn.content) for turn in self.history) > 2_400:
            raise ValueError("history exceeds 2400 characters")
        return self


class EvalObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    repeat: int = Field(ge=1)
    predicted_intent: Intent | None = None
    predicted_dissatisfied: bool | None = None
    predicted_action: Action | None = None
    latency_ms: float = Field(ge=0)
    error_kind: str | None = None

    @model_validator(mode="after")
    def validate_observation_contract(self) -> EvalObservation:
        predictions = (
            self.predicted_intent,
            self.predicted_dissatisfied,
            self.predicted_action,
        )
        if self.error_kind is not None and any(item is not None for item in predictions):
            raise ValueError("error observations cannot contain predictions")
        if self.error_kind is None and any(item is None for item in predictions):
            raise ValueError("successful observations require all predictions")
        return self

    @property
    def succeeded(self) -> bool:
        return (
            self.error_kind is None
            and self.predicted_intent is not None
            and self.predicted_dissatisfied is not None
            and self.predicted_action is not None
        )


def load_cases(path: Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    seen_ids: set[str] = set()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            case = EvalCase.model_validate_json(raw_line)
        except ValidationError:
            raise ValueError(f"invalid evaluation case at line {line_number}") from None
        if case.id in seen_ids:
            raise ValueError(f"duplicate evaluation case id at line {line_number}")
        seen_ids.add(case.id)
        cases.append(case)
    if not cases:
        raise ValueError("evaluation dataset is empty")
    return cases


async def run_evaluation(
    *,
    settings: Settings,
    cases: Sequence[EvalCase],
    repeats: int,
    concurrency: int,
    progress: bool = True,
    gateway: LLMGateway | None = None,
) -> tuple[list[EvalObservation], float]:
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    active_gateway = gateway or build_llm_gateway(settings)
    semaphore = asyncio.Semaphore(concurrency)
    completed = 0
    total = len(cases) * repeats
    started_batch = time.perf_counter()

    async def evaluate_one(case: EvalCase, repeat: int) -> EvalObservation:
        nonlocal completed
        async with semaphore:
            started = time.perf_counter()
            try:
                decision = await active_gateway.decide(
                    case.message,
                    history=tuple(
                        DialogueTurn(role=turn.role, content=turn.content) for turn in case.history
                    ),
                )
            except LLMUnavailableError:
                observation = EvalObservation(
                    case_id=case.id,
                    repeat=repeat,
                    latency_ms=(time.perf_counter() - started) * 1_000,
                    error_kind="unavailable",
                )
            except LLMProtocolError:
                observation = EvalObservation(
                    case_id=case.id,
                    repeat=repeat,
                    latency_ms=(time.perf_counter() - started) * 1_000,
                    error_kind="protocol",
                )
            except Exception as error:
                observation = EvalObservation(
                    case_id=case.id,
                    repeat=repeat,
                    latency_ms=(time.perf_counter() - started) * 1_000,
                    error_kind=f"unexpected:{type(error).__name__}",
                )
            else:
                observation = EvalObservation(
                    case_id=case.id,
                    repeat=repeat,
                    predicted_intent=decision.intent,
                    predicted_dissatisfied=decision.dissatisfied,
                    predicted_action=decision.action,
                    latency_ms=(time.perf_counter() - started) * 1_000,
                )
        completed += 1
        if progress:
            status = observation.error_kind or "ok"
            print(
                f"[{completed:>3}/{total}] {case.id} repeat={repeat} {status}",
                flush=True,
            )
        return observation

    tasks = [
        asyncio.create_task(evaluate_one(case, repeat))
        for case in cases
        for repeat in range(1, repeats + 1)
    ]
    try:
        observations = await asyncio.gather(*tasks)
    finally:
        await active_gateway.aclose()
    elapsed = time.perf_counter() - started_batch
    return sorted(observations, key=lambda item: (item.case_id, item.repeat)), elapsed


def compute_metrics(
    cases: Sequence[EvalCase],
    observations: Sequence[EvalObservation],
    *,
    repeats: int,
    batch_elapsed_seconds: float,
) -> dict[str, Any]:
    case_map = {case.id: case for case in cases}
    total = len(observations)
    succeeded = sum(item.succeeded for item in observations)
    confusion = {
        expected: {predicted: 0 for predicted in [*INTENT_LABELS, ERROR_LABEL]}
        for expected in INTENT_LABELS
    }
    action_confusion = {
        expected: {predicted: 0 for predicted in [*ACTION_LABELS, ERROR_LABEL]}
        for expected in ACTION_LABELS
    }
    canonical_correct = 0
    accepted_correct = 0
    action_correct = 0
    joint_correct = 0
    dissatisfaction_correct = 0
    diss_tp = diss_tn = diss_fp = diss_fn = diss_errors = 0

    by_language: dict[str, list[bool]] = defaultdict(list)
    by_tag: dict[str, list[bool]] = defaultdict(list)
    error_case_ids: set[str] = set()
    intent_mismatch_ids: set[str] = set()
    joint_mismatch_ids: set[str] = set()

    for observation in observations:
        case = case_map[observation.case_id]
        predicted_intent = (
            observation.predicted_intent.value
            if observation.predicted_intent is not None
            else ERROR_LABEL
        )
        confusion[case.canonical_intent.value][predicted_intent] += 1
        predicted_action_label = (
            observation.predicted_action.value
            if observation.predicted_action is not None
            else ERROR_LABEL
        )
        action_confusion[case.accepted_actions[0].value][predicted_action_label] += 1
        canonical_match = observation.predicted_intent is case.canonical_intent
        accepted_match = (
            observation.predicted_intent in case.accepted_intents
            if observation.predicted_intent is not None
            else False
        )
        action_match = (
            observation.predicted_action in case.accepted_actions
            if observation.predicted_action is not None
            else False
        )
        dissatisfied_match = (
            observation.predicted_dissatisfied is case.dissatisfied
            if observation.predicted_dissatisfied is not None
            else False
        )
        joint_match = accepted_match and action_match and dissatisfied_match
        canonical_correct += canonical_match
        accepted_correct += accepted_match
        action_correct += action_match
        dissatisfaction_correct += dissatisfied_match
        joint_correct += joint_match
        by_language[case.language].append(joint_match)
        for tag in case.tags:
            by_tag[tag].append(joint_match)
        if observation.error_kind:
            error_case_ids.add(case.id)
        if not accepted_match:
            intent_mismatch_ids.add(case.id)
        if not joint_match:
            joint_mismatch_ids.add(case.id)

        predicted_dissatisfied = observation.predicted_dissatisfied
        if predicted_dissatisfied is None:
            diss_errors += 1
            if case.dissatisfied:
                diss_fn += 1
        elif case.dissatisfied and predicted_dissatisfied is True:
            diss_tp += 1
        elif not case.dissatisfied and predicted_dissatisfied is False:
            diss_tn += 1
        elif case.dissatisfied:
            diss_fn += 1
        else:
            diss_fp += 1

    per_intent: dict[str, dict[str, float | int]] = {}
    for label in INTENT_LABELS:
        tp = confusion[label][label]
        fp = sum(confusion[row][label] for row in INTENT_LABELS if row != label)
        fn = sum(count for predicted, count in confusion[label].items() if predicted != label)
        precision, recall, f1 = _precision_recall_f1(tp, fp, fn)
        per_intent[label] = {
            "support": sum(confusion[label].values()),
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
    macro_f1 = sum(float(item["f1"]) for item in per_intent.values()) / len(per_intent)
    diss_precision, diss_recall, diss_f1 = _precision_recall_f1(diss_tp, diss_fp, diss_fn)

    grouped: dict[str, list[EvalObservation]] = defaultdict(list)
    for observation in observations:
        grouped[observation.case_id].append(observation)
    unanimous = 0
    majority_shares: list[float] = []
    disagreement_ids: list[str] = []
    for case in cases:
        case_observations = grouped[case.id]
        values: list[tuple[str, bool, str]] = []
        for item in case_observations:
            if not item.succeeded:
                continue
            assert item.predicted_intent is not None
            assert item.predicted_dissatisfied is not None
            assert item.predicted_action is not None
            values.append(
                (
                    item.predicted_intent.value,
                    item.predicted_dissatisfied,
                    item.predicted_action.value,
                )
            )
        counts = Counter(values)
        majority_share = max(counts.values(), default=0) / repeats
        majority_shares.append(majority_share)
        case_unanimous = (
            len(case_observations) == repeats
            and all(item.succeeded for item in case_observations)
            and len(counts) == 1
        )
        unanimous += case_unanimous
        if not case_unanimous:
            disagreement_ids.append(case.id)

    all_latencies = [item.latency_ms for item in observations]
    success_latencies = [item.latency_ms for item in observations if item.succeeded]
    return {
        "case_count": len(cases),
        "repeat_count": repeats,
        "trial_count": total,
        "successful_trials": succeeded,
        "strict_contract_success_rate": _ratio(succeeded, total),
        "canonical_intent_accuracy": _ratio(canonical_correct, total),
        "accepted_intent_accuracy": _ratio(accepted_correct, total),
        "intent_macro_f1": macro_f1,
        "per_intent": per_intent,
        "intent_confusion": confusion,
        "dissatisfaction": {
            "tp": diss_tp,
            "tn": diss_tn,
            "fp": diss_fp,
            "fn": diss_fn,
            "errors": diss_errors,
            "accuracy": _ratio(dissatisfaction_correct, total),
            "precision": diss_precision,
            "recall": diss_recall,
            "f1": diss_f1,
        },
        "accepted_action_accuracy": _ratio(action_correct, total),
        "action_confusion": action_confusion,
        "joint_accepted_accuracy": _ratio(joint_correct, total),
        "by_language": {
            key: {
                "trials": len(values),
                "joint_accuracy": _ratio(sum(values), len(values)),
            }
            for key, values in sorted(by_language.items())
        },
        "by_tag": {
            key: {
                "trials": len(values),
                "joint_accuracy": _ratio(sum(values), len(values)),
            }
            for key, values in sorted(by_tag.items())
        },
        "consistency": {
            "unanimous_cases": unanimous,
            "unanimous_rate": _ratio(unanimous, len(cases)),
            "mean_majority_share": (
                sum(majority_shares) / len(majority_shares) if majority_shares else 0.0
            ),
            "disagreement_case_ids": sorted(disagreement_ids),
        },
        "latency_ms": {
            "all_p50": _nearest_rank(all_latencies, 0.50),
            "all_p95": _nearest_rank(all_latencies, 0.95),
            "success_p50": _nearest_rank(success_latencies, 0.50),
            "success_p95": _nearest_rank(success_latencies, 0.95),
        },
        "batch_elapsed_seconds": batch_elapsed_seconds,
        "throughput_trials_per_second": _ratio(total, batch_elapsed_seconds),
        "error_case_ids": sorted(error_case_ids),
        "intent_mismatch_case_ids": sorted(intent_mismatch_ids),
        "joint_mismatch_case_ids": sorted(joint_mismatch_ids),
    }


def render_markdown_report(
    *,
    settings: Settings,
    dataset_path: Path,
    cases: Sequence[EvalCase],
    metrics: dict[str, Any],
    repeats: int,
    concurrency: int,
) -> str:
    lines = [
        "# 模型评测报告",
        "",
        f"- 运行时间：{datetime.now(UTC).isoformat()}",
        f"- Provider：{settings.llm_provider}",
        f"- Endpoint：{_provider_endpoint(settings)}",
        f"- 模型：{settings.active_model}",
        f"- 数据集：{dataset_path.as_posix()}（{len(cases)} cases）",
        f"- 重复 / 并发：{repeats} / {concurrency}",
        f"- 逻辑分类 trials：{metrics['trial_count']}，"
        f"批次耗时：{metrics['batch_elapsed_seconds']:.1f}s",
        "",
        "## 核心结果",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        f"| Strict contract 成功率 | {_pct(metrics['strict_contract_success_rate'])} |",
        f"| Canonical intent accuracy | {_pct(metrics['canonical_intent_accuracy'])} |",
        f"| Ambiguity-aware intent accuracy | {_pct(metrics['accepted_intent_accuracy'])} |",
        f"| Intent macro-F1 | {_pct(metrics['intent_macro_f1'])} |",
        f"| Dissatisfied F1 / recall | {_pct(metrics['dissatisfaction']['f1'])} / "
        f"{_pct(metrics['dissatisfaction']['recall'])} |",
        f"| Dissatisfied ERROR trials | {metrics['dissatisfaction']['errors']} |",
        f"| Accepted action accuracy | {_pct(metrics['accepted_action_accuracy'])} |",
        f"| Joint accepted accuracy | {_pct(metrics['joint_accepted_accuracy'])} |",
        f"| Case unanimous rate | {_pct(metrics['consistency']['unanimous_rate'])} |",
        f"| Mean majority share | {_pct(metrics['consistency']['mean_majority_share'])} |",
        f"| 分类成功延迟 p50 / p95 | {metrics['latency_ms']['success_p50']:.0f}ms / "
        f"{metrics['latency_ms']['success_p95']:.0f}ms |",
        "",
        "## 各意图指标",
        "",
        "| Intent | Support | Precision | Recall | F1 |",
        "|---|---:|---:|---:|---:|",
    ]
    for label in INTENT_LABELS:
        item = metrics["per_intent"][label]
        lines.append(
            f"| {label} | {item['support']} | {_pct(item['precision'])} | "
            f"{_pct(item['recall'])} | {_pct(item['f1'])} |"
        )

    columns = [*INTENT_LABELS, ERROR_LABEL]
    lines.extend(
        [
            "",
            "## Intent 混淆矩阵",
            "",
            "| expected / predicted | " + " | ".join(columns) + " |",
            "|---|" + "---:|" * len(columns),
        ]
    )
    for expected in INTENT_LABELS:
        row = metrics["intent_confusion"][expected]
        lines.append(f"| {expected} | " + " | ".join(str(row[column]) for column in columns) + " |")

    action_columns = [*ACTION_LABELS, ERROR_LABEL]
    lines.extend(
        [
            "",
            "## Action 混淆矩阵",
            "",
            "| expected / predicted | " + " | ".join(action_columns) + " |",
            "|---|" + "---:|" * len(action_columns),
        ]
    )
    for expected in ACTION_LABELS:
        row = metrics["action_confusion"][expected]
        lines.append(
            f"| {expected} | " + " | ".join(str(row[column]) for column in action_columns) + " |"
        )

    lines.extend(
        [
            "",
            "## 语言切片",
            "",
            "| Language | Trials | Joint accuracy |",
            "|---|---:|---:|",
        ]
    )
    for language, item in metrics["by_language"].items():
        lines.append(f"| {language} | {item['trials']} | {_pct(item['joint_accuracy'])} |")

    lines.extend(
        [
            "",
            "## 模型标签切片（非端到端安全率）",
            "",
            "下表只衡量 intent + dissatisfied + action 标签；adversarial / injection "
            "切片不衡量回复泄漏、越权副作用或完整攻击链路。",
            "",
            "| Tag | Trials | Joint accuracy |",
            "|---|---:|---:|",
        ]
    )
    for tag, item in metrics["by_tag"].items():
        if item["trials"] < repeats * 4:
            continue
        lines.append(f"| {tag} | {item['trials']} | {_pct(item['joint_accuracy'])} |")

    lines.extend(
        [
            "",
            "## 一致性与错误分析",
            "",
            f"- 不一致 case：{_id_list(metrics['consistency']['disagreement_case_ids'])}",
            f"- Provider / schema 错误 case：{_id_list(metrics['error_case_ids'])}",
            f"- Intent 越出接受边界：{_id_list(metrics['intent_mismatch_case_ids'])}",
            f"- 联合标签不匹配：{_id_list(metrics['joint_mismatch_case_ids'])}",
            "",
            "## 复现",
            "",
            "~~~bash",
            *_reproduction_lines(settings, dataset_path, repeats=repeats, concurrency=concurrency),
            "~~~",
            "",
            "## 证据边界",
            "",
            "- 这是同一开发/回归集在指定模型、Provider 和运行时点上的概率性质量评测，"
            "不是未见集泛化或形式化保证。",
            "- Joint accepted accuracy 只检查接受意图 + dissatisfied + 接受动作，"
            "不检查生成回复或端到端副作用。",
            "- 延迟仅覆盖 decide 分类调用（含 Gateway 内部重试），不包含 reviewer、"
            "SQLite、状态机或完整 HTTP 业务链路。",
            "- 报告与机器结果不保存消息、历史、reply draft、rationale、异常正文、"
            "请求头或 API Key。",
            "- 动作白名单、并发限流、状态机、静默、epoch 与 fail-closed "
            "由默认 pytest 证明，不以本评测替代。",
            "- 自然语言防套话仍不承诺 100%；完整攻击链路证据见 "
            "ATTACK-RESULTS.md 与 LIVE-ACCEPTANCE.md。",
            "- Prompt 与 Provider 配置如何由误差分析迭代，见 EVAL-ITERATION.md。",
            "",
        ]
    )
    return "\n".join(lines)


def sanitized_results_payload(
    *,
    settings: Settings,
    dataset_path: Path,
    observations: Sequence[EvalObservation],
    metrics: dict[str, Any],
    repeats: int,
    concurrency: int,
) -> dict[str, Any]:
    return {
        "metadata": {
            "generated_at": datetime.now(UTC).isoformat(),
            "provider": settings.llm_provider,
            "endpoint_host": _provider_endpoint(settings),
            "model": settings.active_model,
            "dataset": dataset_path.as_posix(),
            "repeats": repeats,
            "concurrency": concurrency,
        },
        "metrics": metrics,
        "observations": [observation.model_dump(mode="json") for observation in observations],
    }


def _precision_recall_f1(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    f1 = _ratio(2 * precision * recall, precision + recall)
    return precision, recall, f1


def _ratio(numerator: float | int, denominator: float | int) -> float:
    return float(numerator / denominator) if denominator else 0.0


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _id_list(values: Sequence[str]) -> str:
    return ", ".join(values) if values else "无"


def _provider_endpoint(settings: Settings) -> str:
    if settings.llm_provider == "gemini":
        return "generativelanguage.googleapis.com"
    return urlsplit(settings.llm_api_base).netloc


def _reproduction_lines(
    settings: Settings,
    dataset_path: Path,
    *,
    repeats: int,
    concurrency: int,
) -> list[str]:
    if settings.llm_provider == "gemini":
        provider_lines = [
            "LLM_PROVIDER=gemini \\",
            "GEMINI_API_KEY='仅通过环境变量注入' \\",
            f"GEMINI_MODEL={settings.active_model} \\",
        ]
    else:
        provider_lines = [
            "LLM_PROVIDER=openai_compatible \\",
            f"LLM_API_BASE={settings.llm_api_base} \\",
            "LLM_API_KEY='仅通过环境变量注入' \\",
            f"LLM_MODEL={settings.active_model} \\",
        ]
    return [
        *provider_lines,
        "uv run python -m leadguard.evaluation \\",
        f"  --dataset {dataset_path.as_posix()} --repeats {repeats} --concurrency {concurrency}",
    ]


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _dataset_summary(cases: Sequence[EvalCase]) -> str:
    intents = Counter(case.canonical_intent.value for case in cases)
    dissatisfied = Counter(case.dissatisfied for case in cases)
    languages = Counter(case.language for case in cases)
    return (
        f"cases={len(cases)} intents={dict(sorted(intents.items()))} "
        f"dissatisfied={dict(sorted(dissatisfied.items()))} "
        f"languages={dict(sorted(languages.items()))}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a sanitized, repeatable LLM classification evaluation."
    )
    parser.add_argument("--dataset", type=Path, default=Path("evals/model_cases.jsonl"))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--report", type=Path, default=Path("docs/EVAL-REPORT.md"))
    parser.add_argument("--json-out", type=Path, default=Path("docs/eval-results.json"))
    return parser


async def _run_from_args(args: argparse.Namespace) -> int:
    cases = load_cases(args.dataset)
    if args.case_id:
        requested = set(args.case_id)
        available = {case.id for case in cases}
        missing = sorted(requested - available)
        if missing:
            raise ValueError("unknown case ids: " + ", ".join(missing))
        cases = [case for case in cases if case.id in requested]
    if args.limit is not None:
        if args.limit < 1:
            raise ValueError("limit must be at least 1")
        cases = cases[: args.limit]
    print(_dataset_summary(cases), flush=True)
    if args.validate_only:
        return 0
    settings = Settings()
    if not settings.llm_configured:
        raise ValueError("the selected LLM provider is not configured")
    observations, elapsed = await run_evaluation(
        settings=settings,
        cases=cases,
        repeats=args.repeats,
        concurrency=args.concurrency,
    )
    metrics = compute_metrics(
        cases,
        observations,
        repeats=args.repeats,
        batch_elapsed_seconds=elapsed,
    )
    report = render_markdown_report(
        settings=settings,
        dataset_path=args.dataset,
        cases=cases,
        metrics=metrics,
        repeats=args.repeats,
        concurrency=args.concurrency,
    )
    payload = sanitized_results_payload(
        settings=settings,
        dataset_path=args.dataset,
        observations=observations,
        metrics=metrics,
        repeats=args.repeats,
        concurrency=args.concurrency,
    )
    _atomic_write(args.report, report)
    _atomic_write(
        args.json_out,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    print(f"report={args.report} json={args.json_out}", flush=True)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(_run_from_args(args))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
