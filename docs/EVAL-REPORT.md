# 模型评测报告

- 运行时间：2026-08-18T02:00:01.440813+00:00
- Provider：openai_compatible
- Endpoint：api.openlux.ai
- 模型：gpt-5.6-luna
- 数据集：evals/model_cases.jsonl（60 cases）
- 重复 / 并发：3 / 2
- 逻辑分类 trials：180，批次耗时：365.6s

## 核心结果

| 指标 | 结果 |
|---|---:|
| Strict contract 成功率 | 100.0% |
| Canonical intent accuracy | 99.4% |
| Ambiguity-aware intent accuracy | 100.0% |
| Intent macro-F1 | 99.4% |
| Dissatisfied F1 / recall | 100.0% / 100.0% |
| Dissatisfied ERROR trials | 0 |
| Accepted action accuracy | 100.0% |
| Joint accepted accuracy | 100.0% |
| Case unanimous rate | 98.3% |
| Mean majority share | 99.4% |
| 分类成功延迟 p50 / p95 | 3096ms / 6242ms |

## 各意图指标

| Intent | Support | Precision | Recall | F1 |
|---|---:|---:|---:|---:|
| interested | 36 | 100.0% | 100.0% | 100.0% |
| needs_more_info | 36 | 97.3% | 100.0% | 98.6% |
| rejected | 36 | 100.0% | 100.0% | 100.0% |
| off_topic | 36 | 100.0% | 100.0% | 100.0% |
| other | 36 | 100.0% | 97.2% | 98.6% |

## Intent 混淆矩阵

| expected / predicted | interested | needs_more_info | rejected | off_topic | other | ERROR |
|---|---:|---:|---:|---:|---:|---:|
| interested | 36 | 0 | 0 | 0 | 0 | 0 |
| needs_more_info | 0 | 36 | 0 | 0 | 0 | 0 |
| rejected | 0 | 0 | 36 | 0 | 0 | 0 |
| off_topic | 0 | 0 | 0 | 36 | 0 | 0 |
| other | 0 | 1 | 0 | 0 | 35 | 0 |

## Action 混淆矩阵

| expected / predicted | reply | schedule_followup | escalate_to_human | mark_not_interested | ERROR |
|---|---:|---:|---:|---:|---:|
| reply | 99 | 0 | 0 | 0 | 0 |
| schedule_followup | 0 | 33 | 0 | 0 | 0 |
| escalate_to_human | 0 | 0 | 12 | 0 | 0 |
| mark_not_interested | 0 | 0 | 0 | 36 | 0 |

## 语言切片

| Language | Trials | Joint accuracy |
|---|---:|---:|
| en | 15 | 100.0% |
| es | 15 | 100.0% |
| ja | 15 | 100.0% |
| zh | 135 | 100.0% |

## 模型标签切片（非端到端安全率）

下表只衡量 intent + dissatisfied + action 标签；adversarial / injection 切片不衡量回复泄漏、越权副作用或完整攻击链路。

| Tag | Trials | Joint accuracy |
|---|---:|---:|
| adversarial | 24 | 100.0% |
| ambiguous | 21 | 100.0% |
| context | 15 | 100.0% |
| explicit_rejection | 15 | 100.0% |
| human_requested | 12 | 100.0% |
| later_contact | 33 | 100.0% |
| multilingual | 45 | 100.0% |
| orthogonal_dissatisfaction | 45 | 100.0% |
| role_spoofing | 12 | 100.0% |
| unknown_action | 12 | 100.0% |
| unknown_fact | 12 | 100.0% |
| unrelated_question | 15 | 100.0% |

## 一致性与错误分析

- 不一致 case：T05
- Provider / schema 错误 case：无
- Intent 越出接受边界：无
- 联合标签不匹配：无

## 复现

~~~bash
LLM_PROVIDER=openai_compatible \
LLM_API_BASE=https://api.openlux.ai/v1 \
LLM_API_KEY='仅通过环境变量注入' \
LLM_MODEL=gpt-5.6-luna \
uv run python -m leadguard.evaluation \
  --dataset evals/model_cases.jsonl --repeats 3 --concurrency 2
~~~

## 证据边界

- 这是同一开发/回归集在指定模型、Provider 和运行时点上的概率性质量评测，不是未见集泛化或形式化保证。
- Joint accepted accuracy 只检查接受意图 + dissatisfied + 接受动作，不检查生成回复或端到端副作用。
- 延迟仅覆盖 decide 分类调用（含 Gateway 内部重试），不包含 reviewer、SQLite、状态机或完整 HTTP 业务链路。
- 报告与机器结果不保存消息、历史、reply draft、rationale、异常正文、请求头或 API Key。
- 动作白名单、并发限流、状态机、静默、epoch 与 fail-closed 由默认 pytest 证明，不以本评测替代。
- 自然语言防套话仍不承诺 100%；完整攻击链路证据见 ATTACK-RESULTS.md 与 LIVE-ACCEPTANCE.md。
- Prompt 与 Provider 配置如何由误差分析迭代，见 EVAL-ITERATION.md。
