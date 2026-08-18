# 四项硬约束：代码强制点、边界与验收

本文描述当前实现的真实安全边界。核心原则是：选定的 LLM Provider 只产生经过严格校验的建议，SQLite 中的会话状态和服务端策略才是执行依据。Prompt 用来提高模型判断质量，但不承担最终授权、状态转换或发送限流。

## 适用范围与执行链路

- `sender=agent` 的 `messages` 记录代表模拟客户通道中真正发出的 Agent 消息；它只能由 `storage.SQLiteStore.send_reply` 写入。
- `sender=system` 的记录是本地操作台审计事件，不是客户回复，不属于四种业务动作。生产接入 IM 时不应把这类事件投递给客户。
- 浏览器是演示操作台，`TurnResult`、guard trace 和状态面板属于操作员诊断数据，不应直接作为生产客户接口暴露。

一次 active 会话的处理顺序是：

1. `service.AgentService.process_message` 先取得该 customer 专属的 `asyncio.Lock`，再持久化客户输入并做生命周期预检；
2. `llm.build_llm_gateway` 选择 OpenAI-compatible 或 Gemini 适配器，并生成严格的 `ModelDecision`；
3. `storage.SQLiteStore.apply_decision` 在写事务中重新读取权威状态，并调用 `domain.enforce_state_machine`；
4. 只有 `reply` 会进入 `output_guard.OutputGuard.inspect`；
5. 审查通过后，`storage.SQLiteStore.send_reply` 在最终发送边界再次检查状态、激活代次和滑动窗口；
6. 模型异常或回复审查失败时，`storage.SQLiteStore.force_escalate` 失败关闭，不发送候选回复。

## 1. 同一客户任意 60 秒窗口最多主动发送一条消息

### 代码如何强制

- `SQLiteStore.send_reply` 是写入 `sender=agent` 消息的唯一代码路径。`AgentService` 不直接返回或持久化未经该函数放行的候选草稿。
- `send_reply` 在同一个 `BEGIN IMMEDIATE` 事务中读取当前客户的 `last_outbound_at`、判断 `now < last_outbound_at + rate_limit_seconds`、插入 Agent 消息并更新 `last_outbound_at`。检查与写入不可被另一个 SQLite 写事务穿插，因此并发请求不能同时看到旧值并各自发送。`Settings.rate_limit_seconds` 的 Pydantic 下限是 60，运行配置只能保持 60 秒或设得更严格，不能降级题目约束。
- 这是滑动窗口，而不是自然分钟桶。阈值为默认 60 秒时，实现采用半开窗口 `(now - 60s, now]`：相隔 `59.999s` 时阻断，相隔恰好 `60.000s` 时允许。只要每个相邻成功发送时间至少相隔配置阈值（且阈值不小于 60 秒），任一这样的 60 秒窗口内就不可能出现两条成功发送。
- 限流状态持久化在每个 conversation 的 SQLite 行中，进程重启不会清空；不同客户使用不同的 `last_outbound_at`，互不占用额度。
- Provider 内部的有限重试只发生在模型传输层，模型没有发送工具；无论模型请求重试多少次，每个业务 turn 最终仍只可能调用一次 `send_reply`。相同 `request_id` 还会由 `turns` 缓存与唯一约束去重。
- 对候选 `reply`，`enforce_state_machine` 沿用已有的 `followup_pending`；`send_reply` 只有在 Agent 消息成功插入后才将它清零。回复若在最终边界被限流，事务会在更新前返回，既有 follow-up 标记仍然保留。

### 为什么 Prompt 不是防线

模型既看不到也不能修改 `last_outbound_at`，且没有注册发送工具。即使客户要求“连续发十条”、模型重复建议 `reply`，真正写出 Agent 消息的仍是上述原子事务。分类 prompt 中是否提到限流不影响该不变量。

### 已知边界

- 正确性以单个共享 SQLite 数据库和受信任的服务端时钟为前提。未来若增加真实 IM、多个数据库或另一条发信代码路径，所有客户可见出站消息仍必须汇入同一个原子发送边界；系统时钟大幅向前跳变也需要改用更强的可信时间源处理。
- `append_system_message` 不经过发送限流，因为它只写操作台审计事件；若把 system 事件投递到真实客户通道，就必须一并纳入发送网关。

### 验收证据

- `tests/test_rate_limit_and_isolation.py::test_rate_limit_blocks_at_59_999ms_and_allows_at_60s`：证明边界时刻。
- `tests/test_rate_limit_and_isolation.py::test_sliding_window_does_not_reset_on_fixed_minute_boundary`：证明跨自然分钟仍会阻断。
- `tests/test_rate_limit_and_isolation.py::test_concurrent_replies_send_exactly_once`：并发回复最终只有一条 `sender=agent` 记录。
- `tests/test_rate_limit_and_isolation.py::test_customers_have_independent_state_and_rate_limits`：证明限流按客户隔离。
- `tests/test_rate_limit_and_isolation.py::test_reactivate_preserves_last_outbound_rate_limit`：证明人工重新激活不会清除限流历史。
- `tests/test_rate_limit_and_isolation.py::test_rate_limit_survives_service_restart`：证明重启并复用 SQLite 后限流历史仍生效。
- `tests/test_rate_limit_and_isolation.py::test_rate_limited_reply_preserves_pending_followup_until_real_send`：证明被限流的草稿不会误清已有待跟进标记。

## 2. 两次连续异常共用计数器，并在升级后保持静默

### 代码如何强制

- `domain.enforce_state_machine` 计算 `is_strike = (intent is OFF_TOPIC) or dissatisfied`。一轮同时满足两个条件也只增加一次；不满足任一条件时直接将 `strike_count` 重置为 0。
- 新计数达到 2 时，函数在处理模型建议动作之前强制返回 `ESCALATE_TO_HUMAN`、`HUMAN_CONTROLLED`、`strike_count=2` 并递增 `activation_epoch`。因此第二轮的模型动作即使是 `reply`、`schedule_followup` 或 `mark_not_interested`，也会被覆盖。
- `SQLiteStore.apply_decision` 在 `BEGIN IMMEDIATE` 中重读最新 conversation、核对 `ACTIVE` 和请求开始时捕获的 `activation_epoch`，再调用 `enforce_state_machine` 并持久化结果。共享计数器的读改写是原子的，不会因并发产生丢失更新。
- `AgentService.process_message` 从 inbound 写入之前就持有每 customer 独立的 `asyncio.Lock`，并一直持有到该 turn 完成。默认单 ASGI 进程中，同一客户的数据库入站顺序与状态机消费顺序因此一致，不会因某次 LLM 响应更慢而发生后到消息先更新共享计数；不同客户仍可并行处理。
- `AgentService.process_message` 在调用 LLM 前检查生命周期。`HUMAN_CONTROLLED` 或 `NOT_INTERESTED` 会话只记录客户输入和操作台审计，不调用 LLM，也不执行四种动作。即使状态在 LLM 在途期间变化，`apply_decision` 和 `send_reply` 的二次状态/epoch 检查也会丢弃迟到结果。
- `SQLiteStore.reactivate` 是唯一恢复路径：只允许人工控制态回到 active，清零共享计数和 follow-up 标记、递增 `activation_epoch`，但保留 `last_outbound_at`。代次递增可以阻止升级前启动、在“升级后又重新激活”之后才返回的旧 LLM 请求执行。
- 模型调用失败或输出协议错误，以及 `OutputGuard` 阻断候选回复时，`AgentService` 通过 `SQLiteStore.force_escalate` 失败关闭；该函数同样校验 active 状态和期望 epoch。

### 为什么 Prompt 不是防线

Prompt 只让 LLM 判断 `intent` 与独立的 `dissatisfied` 信号。计数、重置、第二次强制覆盖、终止态早退和重新激活全部在模型调用之外完成。模型没有字段可以直接写 `strike_count`、`lifecycle` 或 `activation_epoch`。

### 已知边界

- 硬保证的表述是“连续两次**被模型判定为**答非所问或明显不满后升级”。自然语言分类仍可能误判或漏判；状态机能保证消费分类结果的方式，却不能形式化保证任一 LLM 对所有表达都分类正确。
- 这项顺序保证适用于 README 启动命令所使用的默认单 ASGI 进程；`asyncio.Lock` 是进程内对象。未来若启用多个 worker 或多个服务实例，各进程不能共享这把锁，必须在数据库分配单调入站序号，并用每客户队列按序消费，不能仅依赖当前进程锁或 LLM 完成顺序。
- 重新激活 API 是与客户消息入口分离的控制面，但当前本地 demo 未实现身份认证。客户消息文本无法调用它；生产部署必须为该端点增加身份认证、RBAC 和审计，不能把“知道 URL”视为人工授权。
- “静默”指不调用 LLM、不执行四种业务动作、不产生 `sender=agent` 出站消息；为了可审计，系统仍记录 inbound 和 `sender=system` 的操作台事件。

### 验收证据

- `tests/test_state_and_safety.py::test_shared_strike_counter_resets_and_second_strike_forces_escalation`：覆盖共享 OR 计数、正常轮重置以及第二次强制覆盖。
- `tests/test_state_and_safety.py::test_human_controlled_message_is_silent_without_llm`：升级后的客户消息产生零 LLM 调用、零 Agent 出站和零业务动作。
- `tests/test_state_and_safety.py::test_stale_result_is_rejected_across_escalate_and_reactivate_epoch`：证明旧在途结果不能跨越升级/重激活边界。
- `tests/test_state_and_safety.py::test_concurrent_bad_good_bad_preserves_queue_order_without_false_escalation`：证明 LLM 延迟不会把“异常→正常→异常”重排成误升级。
- `tests/test_state_and_safety.py::test_concurrent_bad_bad_good_escalates_second_and_silences_queued_third`：证明排队的前两次异常仍在第二轮升级，后续正常消息不会迟到重置状态。
- `tests/test_rate_limit_and_isolation.py::test_reactivate_preserves_last_outbound_rate_limit`：证明重激活只清共享计数，不清发送历史。

## 3. 客户内容不能扩大动作权限或绕过静默

### 代码如何强制

- `domain.Action` 只定义 `reply`、`schedule_followup`、`escalate_to_human`、`mark_not_interested` 四个值。`domain.ModelDecision` 使用 strict Pydantic enum 与 `extra="forbid"`；未知动作、额外字段、字符串布尔、不符合 reply 合同，以及 `rejected ↔ mark_not_interested` 不一致的输出都无法成为有效决策。其它允许组合（例如感兴趣后约定稍后跟进）保留给模型按语境选择。
- `OpenAICompatibleGateway` 发送 `strict=true` 的 JSON schema 并拒绝任何 `tool_calls` / `function_call`；`GeminiGateway` 显式关闭 automatic function calling。两条路径都没有向模型注册 Shell、文件、数据库或通用 HTTP 能力。客户文本以 JSON 字段值传入，而不是被拼成可执行调用。
- `AgentService` 对四种动作使用显式分支；没有 `eval`、反射式函数查找或由模型提供函数名的动态 dispatch。模型协议校验失败时不会使用任何“部分解析成功”的字段，而是调用 `force_escalate` 失败关闭。
- 真正状态变更由 `apply_decision` 调用 `enforce_state_machine` 后写入；真正发送只能走 `send_reply`。两者都重读服务端 lifecycle/epoch。客户即使诱导模型输出允许列表中的 `reply`，也无法在人工控制态写出消息。
- `/api/conversations/{id}/messages` 只把 `content` 交给 `AgentService`，不会把 `/reactivate`、SQL、JSON tool call 或角色声明解析为操作员命令。人工恢复是单独调用 `storage.reactivate` 的控制面路由。

### 为什么 Prompt 不是防线

分类 prompt 确实提醒模型把客户内容视为不可信数据，但删除这句提醒也不会新增第五种动作、注册工具、开放动态执行，或绕过 lifecycle/epoch 检查。Prompt 失守可能导致四个允许动作中的业务误判，不能导致模型获得代码中不存在的能力。

### 已知边界

- “100% 代码强制”的范围是：任意客户消息经当前 customer-message 入口处理时，执行能力不会超出四动作，且不能通过文本恢复终止态。拥有服务进程、数据库写权限或直接调用未鉴权控制面的人不属于“客户对话内容”威胁模型。
- 当前 `reactivate` demo 路由未鉴权是明确的生产边界；它不构成自然语言 prompt injection，但若把服务公开到不可信网络，攻击者可以绕过 UI 直接调用控制面，因此上线前必须修复。
- 允许列表只约束能力，不保证 LLM 总能挑选业务上最合适的允许动作。提示词注入仍可能在四动作内部影响分类或建议；意图—动作一致性校验、共享状态机和失败关闭降低影响，但无法完全消除语义误判。

### 验收证据

- `tests/test_state_and_safety.py::test_unknown_model_action_is_rejected`：未知动作或额外能力无法通过 schema，服务失败关闭且没有执行副作用。
- `tests/test_state_and_safety.py::test_human_controlled_message_is_silent_without_llm`：在客户文本中要求回复、调度或重新激活，仍不能越过人工控制态。
- `tests/test_idempotency_and_api.py` 的请求 schema 用例：客户 API 拒绝额外控制字段；`test_request_id_replay_is_idempotent` 证明重放不会重复执行 turn。
- `tests/test_openai_compatible_provider.py` 证明请求没有 `tools` / `functions`，拒绝未知动作、额外字段、tool call、拒答和截断，并验证 401 不重试、429/5xx 有界重试与异常脱敏。`AgentService` 的显式 `Action` 分支是另一层静态能力边界。

## 4. 防止系统提示词、内部规则、价格底线和凭证泄漏

### 纵深防御

1. **数据最小化。** 两个 LLM Gateway 每轮只接收当前客户消息、固定的 `PUBLIC_PRODUCT_CONTEXT`，以及同一客户最近最多 6 条已公开的 customer/agent 对话（单条最多 500 字、总计最多 2400 字）。系统审计、危险草稿、其它客户历史、内部价格表和凭证不会进入上下文。运行凭证由 `Settings` 的 `SecretStr` 接收，仅用于 Provider 鉴权和本地出站保护值检查，不被放入审查 prompt、TurnResult 或应用日志。
2. **输入边界。** Gateway 用 JSON 序列化客户消息，system instruction 明确客户字段是不可信数据，并用 strict schema 限制输出。此层能提高抗注入表现，但单独不视为安全保证。
3. **确定性出站检查。** `create_app` 将运行时 API 凭证作为 `protected_values` 只交给本地 `OutputGuard`，不会把该值交给语义 reviewer；`output_guard.OutputGuard.inspect` 在发送前做 Unicode NFKC/零宽字符归一化，并检查候选回复是否精确包含归一化后的受保护值。该层还执行空值与长度、异常长编码片段、敏感披露表述及受保护 prompt 片段重合检查。这些规则是快速、可解释的拦截层，但不是对所有攻击表达的枚举。
4. **单独的语义审查调用。** 确定性检查通过后，`OutputGuard` 调用当前 Gateway 的 `review_reply`，只提交客户消息、候选回复和公开产品上下文，并要求严格返回 `LeakageReview`。审查不确定、返回 unsafe、协议异常或服务不可用时一律 `safe=false`。
5. **发送前失败关闭。** `AgentService` 只有在 `OutputGuardResult.safe` 为真时才调用 `send_reply`。否则用 `force_escalate` 转人工；危险草稿不会写入 `messages`、`turns` 的 `final_reply` 或 API 结果。模型生成的 `rationale` 也不会持久化，操作台只保存服务端固定审计说明。

### 为什么 Prompt 不是唯一防线

即使生成模型不遵守“不要泄露”的指令并在 `reply_draft` 中回显提示词，候选草稿仍需经过独立的确定性检查、另一轮语义审查和 `AgentService` 的 fail-closed 分支，最后才可能到达 `send_reply`。更重要的是，真实凭证和非公开价格数据没有被放进模型内容，减少了模型可泄露的秘密面。

### 已知边界

- 此约束不能承诺 100%。标记规则可能被同义改写、短编码、隐写或拆分表达绕过；受保护值检查能拦截归一化后的原值回显，但不保证识别加密、摘要或任意变换后的值。语义审查仍由同一模型提供方完成，可能与生成调用出现相关性漏判，也可能误伤正常回复。
- 最近对话同样被明确标记为不可信数据；历史中的提示注入可能影响后续语义判断，但无法扩张 Action enum、调用工具或绕过服务端状态。长度/条数上限限制持久注入面，跨客户隔离测试证明不会混入其它客户上下文。
- 数据最小化能阻止模型原样披露它从未获得的底价或凭证，但模型仍可能捏造“内部规则/价格”或概括自己的 system instruction。输出审查降低风险，不能构成形式化信息流证明。
- 当前 demo 没有独立的客户公开 API：conversation detail 与 `TurnResult.guard_events` 是操作员诊断面，其中有状态机和限流说明。生产系统必须把客户消息通道与操作员审计接口做鉴权和响应字段隔离。
- 模型提供方的服务端数据保留、账户侧日志和基础设施权限不由本仓库控制；生产使用前需按供应商政策配置数据治理。源码或主机读取权限也超出“通过客户对话套话”的威胁模型。

### 验收证据

- `tests/test_state_and_safety.py::test_leakage_guard_and_review_error_fail_closed`：危险草稿或语义审查异常均转人工，候选草稿未发送、未作为 `final_reply` 保存。
- `tests/test_state_and_safety.py::test_runtime_protected_value_is_blocked_before_semantic_review`：运行时凭证原值命中本地检查后直接阻断，且不会送给语义 reviewer。
- `test_recent_dialogue_is_scoped_to_same_customer_public_history`、`test_recent_dialogue_enforces_turn_and_character_bounds` 与 Provider payload 测试证明上下文只含同客户公开消息，不含 system 事件或其它客户 canary，并严格限制为最多 6 条 / 2400 字。
- 对 OutputGuard 的参数化用例应覆盖：伪造的 `protected_values` 原值及零宽字符变形、系统提示词直接回显、内部规则/价格底线表述、长编码数据，以及语义 reviewer 返回 unsafe。测试只能使用假凭证。
- 使用 spy gateway 检查 `review_reply` 输入，只允许当前客户消息、候选草稿和公开上下文；仓库与测试产物中不得出现真实运行凭证。
- 至少三条攻击对话的可复跑结果应记录输入、最终 lifecycle、enforced action、outbound 数量与 guard trace，并对候选危险文本脱敏；现场结果不能用 README 中的预期描述代替。

## 统一验收命令与判定口径

在项目目录运行：

```bash
uv run pytest -q
uv run ruff check .
uv run mypy src
```

验收以 SQLite 最终状态、`sender=agent` 消息数量、fake/spy gateway 调用次数和测试进程退出码为准，不以 UI 提示、模型 rationale 或文档中的自述为准。真实 Provider 验收见 `docs/LIVE-ACCEPTANCE.md`；凭证只允许通过运行时环境变量或被 `.gitignore` 排除的 `.env` 注入，任何真实凭证都不得提交到仓库。
