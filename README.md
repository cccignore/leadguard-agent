# LeadGuard Agent Lab

题目二「获客初筛 Agent」的可运行实现：LLM 只给出严格结构化建议；状态、动作、限流、防泄漏和最终发送均由服务端代码裁决。

## 60 秒启动

要求 Python 3.11+ 与 [uv](https://docs.astral.sh/uv/)；Key 仅从环境读取。

```bash
uv sync --all-groups
cp .env.example .env          # 仅编辑 LLM_API_KEY，不要提交 .env
uv run uvicorn leadguard.app:app --host 127.0.0.1 --port 8000
```

题方 Gemini：将 `.env` 改为 `LLM_PROVIDER=gemini` 并填写 `GEMINI_API_KEY` / `GEMINI_MODEL`；默认 OpenLux 路径已完成 live 验收。

打开 <http://127.0.0.1:8000>。可对话、运行 5 组自动隔离的攻击脚本，并查看“模型建议 → 代码覆写 → 最终结果”。人工恢复不清限流历史。当前按单 ASGI 进程运行；勿加 `--workers > 1`。

## 方案与硬约束

选择 Python + FastAPI + SQLite/WAL；直接实现受限 Provider 适配器，不引入通用 Agent 框架。默认 OpenLux + `gpt-5.6-luna`（实测延迟优于 Terra），保留 `google-genai`。strict schema 锁定 5 类意图和 4 动作，不注册工具；最近 6 条同客户公开对话用于解决多轮指代。

| 要求 | 代码强制点 |
|---|---|
| 任意 60 秒最多 1 条 | `send_reply` 原子滑窗；59.999s 阻断、60.000s 允许，并发仅一条落库 |
| 两次连续异常必转人工 | `enforce_state_machine` 用 `off_topic OR dissatisfied` 共享计数，正常轮清零，第二次覆盖模型动作 |
| 转人工后严格静默 | LLM 前早退，提交/发送复检 `lifecycle + activation_epoch` |
| 客户不能越权 | 4 动作 enum；拒绝 tool/function call；无 shell、文件或任意 HTTP 工具 |
| 防内部泄漏 | 秘密不进上下文；本地检查 + 语义审查；失败即转人工且危险草稿不出站 |

完整机制与边界见 [`docs/CONSTRAINTS.md`](docs/CONSTRAINTS.md)；确定性攻击结果见 [`docs/ATTACK-RESULTS.md`](docs/ATTACK-RESULTS.md)；真实 Provider 的 11/11 场景结果见 [`docs/LIVE-ACCEPTANCE.md`](docs/LIVE-ACCEPTANCE.md)。

真实分类模型评测覆盖 60 cases × 3 次：strict / ambiguity-aware intent / 动作 / 联合准确率均 100%，macro-F1 99.4%，p95 6.24s；详见 [`docs/EVAL-REPORT.md`](docs/EVAL-REPORT.md)。

## 验证与取舍

```bash
uv run ruff check . && uv run mypy src/leadguard && uv run pytest
RUN_LIVE_LLM=1 uv run pytest tests/test_live_llm.py tests/test_live_acceptance.py -m live -q -s
```

默认测试覆盖并发、状态顺序、隔离、幂等、Provider 协议与失败关闭。硬约束未删减；按题意不做真实 IM，follow-up 仅持久化标记，本地控制面未做 RBAC。单人参赛与 AI 协作记录、最终 commit 和人工投入请填写 [`COLLAB.md`](COLLAB.md)。
