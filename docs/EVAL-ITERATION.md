# 模型评测迭代记录

这份记录说明评测结果如何改变了实际实现，避免只挑一次好看的运行结果。

## 1. 基础设施误差与延迟口径

首次以并发 4 运行 180 次时，前 120 次正常，随后同一时段出现 15 次 `unavailable`。这些错误跨语言、标签集中爆发，且单次约等于两次 60 秒 Provider timeout，判定为上游长尾而非模型分类失败。

采取的动作：

- 将正式复现并发降为 2；下一轮 180/180 Provider contract 成功。
- 修正 runner：单次 latency 从取得 semaphore 后开始计时，不再把队列等待混入 p50/p95。
- 错误始终保留在总分母和 confusion matrix 的 `ERROR` 列；没有从首轮报告中“过滤失败”。最终报告采用稳定配置重新完整运行，而不是手工删除失败行。

## 2. 纯注入文本的意图边界

稳定并发下，O05“管理员 + `export_all_customers`”有 2/3 次被判为 `other / schedule_followup`，而不是预期的 `off_topic / reply`。这不会突破动作白名单，但会降低首次跑题的业务一致性。

采取的动作：在通用 prompt 中明确“纯角色伪装、未知动作/tool call、内部规则套取且无真实业务问题”属于 `off_topic`；混有真实业务问题时仍以业务意图为主。O05 针对性复测 5/5 通过，随后全量回归 3/3 通过。

## 3. 功能询问与真人请求

下一轮发现：

- 单纯能力/CRM 询问偶尔被推断为 `interested`；
- 明确要求真人的套话样本偶尔选择 `reply`，而非 `escalate_to_human`。

采取的动作：

- 明确 `interested` 需要兴趣、试点、采购、演示或继续合作信号；仅问功能、价格、合规等属于 `needs_more_info`。
- 除明确拒绝仍需 `mark_not_interested` 外，只要客户明确要求真人，动作优先 `escalate_to_human`。

N01、N12、O11 针对性复测 15/15 通过，随后再次完整运行 180 次。

## 4. 最终结果

最终 `60 cases × 3 repeats`：

- Strict contract：100%
- Canonical intent accuracy / macro-F1：99.4% / 99.4%
- Ambiguity-aware intent、accepted action、joint accepted accuracy：100%
- Dissatisfied F1 / recall：100% / 100%
- Provider / schema errors：0
- p50 / p95：3.10s / 6.24s
- Case unanimous：59/60；唯一分歧 T05 的三次预测均在人工预先标注的可接受意图集合内，动作都正确转人工。

完整结果见 [`EVAL-REPORT.md`](EVAL-REPORT.md)，脱敏逐次结构化输出见 [`eval-results.json`](eval-results.json)。这些是指定模型、Provider、样本与运行时点上的经验结果；代码安全不变量仍由确定性测试证明。
