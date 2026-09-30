# ADR-0009: 渠道传输与出站治理不变量

日期：2026-09-30
状态：已接受（P0 与入口/观测面 P1 已实现；长连接 runner、F3 凭据版本化仍是路线）

## 背景

多渠道网关（见 `docs/vibe/2026-09-28-multi-channel-gateway.md`）让外部 IM
平台的用户直接对话 Yuxi 智能体。「直连各平台」的传输形态由平台硬约束决定：
飞书回调 3s 内回 200（重试 15s/5min/1h/6h，最多 4 次，至少一次投递会重复）、
微信系 5s 内响应否则重试 3 次、钉钉 Stream 模式零公网、Telegram
`getUpdates` 单活（双实例互拉 409）。

## 决策

### 六条不变量（review checklist 级）

1. **平台时限决定形态**：入站回调只允许「重放窗口 → 验签 → 幂等落库 → 入队」，
   任何同步业务调用都是事故；企微/公众号 5s 时限以「空串回包 + 异步推送」化解。
2. **平台是至少一次投递**：幂等是适配器义务——每条入站必须有平台级幂等键
   （消息表唯一索引；事件无 MsgId 时合成 `evt:{Event}:{CreateTime}:{FromUser}`）。
3. **平台细节不出 `adapters/`**，只有 `contracts.py` 能跨界。
4. **执行永不另起路径**：一切经 `create_agent_invocation_run_view`，配额/
   usage_ledger/知识冻结/审计自动继承。
5. **出站永远走 outbox**：claim/lease/指数退避/5 次后 DEAD/requeue；
   部分成功推进 `delivered_chunks` 游标（`ChannelPushError.chunks_sent`），
   重试不重复已发分片。
6. **凭据只经 `secret_crypto` + AAD**；webhook 路径令牌只存哈希。

### 传输可用性（长轮询/长连接）

- **单活锁**：每个长轮询/长连接 app 一把 Redis 锁（TTL 60s，每轮 GET 确认同值后
  `EXPIRE` 刷新 TTL——不续期的锁会在 60s 后过期导致双副本轮换互抢），败者
  standby。锁 fail-open——正确性由「游标 + 消息幂等」保证，锁只防双活抖动。
- **游标持久化**：`channel_apps.config.transport_cursor`，仅整批 ingest **零
  hard-failure**（`ingest_channel_envelopes` 返回的异常计数；去重命中/策略跳过
  不算）才前进，且持久化成功后才推进内存游标；失败时本批重拉，已入库的靠消息
  表幂等吞掉——这是「零丢失」语义的兑现条件。重启从游标恢复。
- **失败批退避（N2）**：整批 ingest 失败时游标不前进，`getUpdates` 会立刻返回同一批——
  无退避即成对 Telegram/PG 的紧循环。退避 10s 起指数升级到 300s 封顶，连续 ≥3 轮升级
  为 ERROR 作为告警信号，但**绝不跳批**（零丢失优先于吞吐）。
- **动态 reconcile**：channels 进程监督循环 30s 对账启用应用集合，新建/停用
  免重启容器；健康检查锚定心跳文件（`/tmp/channels_heartbeat`，60s 刷新，
  缺失 >200s 判不健康——「进程活着但循环卡死」可检出）。
- **部署选型**：默认 webhook（可水平扩）；长连接/长轮询解决「无公网入口」，
  代价是单活 + 连接数上限（钉钉 Stream 每应用默认 50 条），不作为默认。
  飞书 WS / 钉钉 Stream runner 属 P1 增强，接入处在监督循环登记新 runner。

### 入口加固

- **异常分流（三分法）**：验签/结构非法 → **403**（来源不可信，停止处理）；
  凭据/DB/解析器内部异常 → **503**（让平台按自己的有界重试预算重推，幂等
  管线保证零副作用——**200 会把瞬时故障变成永久丢失**）；已确收（含策略
  跳过与逐条 ingest 失败后的部分成功）→ 200。ingest 阶段任一信封异常 →
  503（已成功的信封靠消息表幂等，重推不重复处理）。
- **重放防护**：nonce 首见标记（Redis SETNX TTL 15min）**发生在验签成功之后**
  （路径令牌泄漏无法用随机 nonce 灌缓存）。判定语义：**nonce 首见且陈旧 →
  403**（真异常）；**nonce 已见 → 放行进幂等管线**——绝不在幂等落库之前短路
  平台重试（否则首次尝试死于入库前时，平台有限重试再也救不回来，「至少一次
  投递」被降级成「最多一次」）。钉钉 `sign` 是确定性 HMAC，不作 nonce，只做
  时间窗。字段缺失 fail-open；Telegram webhook 防线是 secret_token +
  update_id 幂等，不做窗口判定。
- **来源 IP 白名单**：应用级 `config.ip_allowlist`（CIDR 列表）+ `ip_allowlist_mode`——默认
  `log` 只观察（记 `inbound_ip_blocked` 计数），先看平台真实出口再切 `enforce`，避免白名单
  写错打崩接入；`enforce` 下不匹配即 403，IP 不可解析 fail-closed。取值是
  `X-Forwarded-For` **最右**段（客户端可自带伪造左段，右一跳即网关看到的对端）。
  APISIX `ip-restriction` 仍是第一道；钉钉明文回调模式上线前**必须**配。
- **钉钉加密回调（opt-in）**：配齐 `encoding_aes_key + token + corp_id` 即按官方《回调事件
  消息体加解密》协议处理——`sha1(sort(token,timestamp,nonce,encrypt))` 验签 + AES-CBC
  明文布局（random16 + msg_len 网络序 + msg + receiveid），与微信系同构故复用
  `WeChatCrypto`；URL 校验的 challenge 必须**回密封包**（`{msg_signature,timeStamp,nonce,
  encrypt}`）。未配 `encoding_aes_key` 时仍是明文模式（HMAC 头签名校验），两条路径共用
  同一套事件解析与幂等管线。

### 出站治理

- **令牌桶**：app 级 Redis 固定秒窗，速率取 `adapter.outbound_rate_per_second`
  （feishu 5/wecom 2/wechat_oa 1/dingtalk 2/telegram 20，保守值）。限流等待
  **发生在 DB 会话之外**（认领→限流→投递三段式，避免 PG idle-in-transaction
  与 cron 叠压）；等待 ≤1s 内联消化，更长则改期重投（下个窗口 +2s）且不消耗
  attempts；Redis 不可用 fail-open。
- **429 尊重**：Telegram `parameters.retry_after` → `next_attempt_at =
  max(指数退避, retry_after)`；微信系频控错误码（45009/45002 等）不可盲目重试。
- **分片续传**：`payload.delivered_chunks` 游标 + 适配器 `start_chunk` 参数；
  片间按 1/rate 节流。**token 失效重取后从 `position` 游标续发**（不回卷
  start_chunk——否则刷新前的已成功片会被重发）。优先让平台「一条装下」，
  长答才分片。
- **可达性终态**：钉钉 sessionWebhook 过期（~2h）、公众号超 48h 客服窗口
  → `retryable=False` 直接 DEAD，`last_error` 以 `undeliverable:` 前缀标记
  （与可重试失败可区分）；管理页可查可重放。

### 已拍板的两个产品决策

- **E4 网页链接**：仅对个人绑定用户（run 归属 ≠ 服务账号）附带
  `{YUXI_PUBLIC_WEB_URL}/agent/{thread_id}`；服务账号会话不推裸链接（无账号
  用户必然 404）。短时效签名链接/公开只读渲染列 P2。
- **F6 PII 摘要口径**：入站 `content_digest` 默认存 `sha256:` 前 16 位；
  `config.store_text_preview=true` 才存前 80 字明文预览（该开关在 API config
  白名单内，未知 config key 一律 422——杜绝「静默 no-op」配置事故）。出站
  （我方生成内容）始终保留预览。
- **限额口径（S4）**：渠道日限额只计 `status='dispatched'`（真实派起 run 的
  消息）；`/help`、`/bind`、非文本提示不占额度。非文本提示与指令在策略门
  **之后**处理——停用渠道/白名单外会话不产生任何出站回复。

### 平台能力矩阵（五平台差异的收口处）

差异只允许停在 `adapters/` + `render.py` 两处，且必须是**闭集**——否则新增平台会静默
继承错误口径（历史上「Markdown 剥离」只对公众号生效，飞书/Telegram 把 `**加粗**`、
表格竖线原样发给用户）。

| 维度 | feishu | wecom | wechat_oa | dingtalk | telegram |
| --- | --- | --- | --- | --- | --- |
| Markdown 呈现 | `card`（卡片富文本） | `native` | `plain`（剥离） | `native` | `plain`（剥离） |
| 群内 @ 判定 | 正文含 bot open_id（`bot_identity_cache`，管理路径预热） | 明文/@提及 | 无群语义 | 明文/@提及 | `bot_command` 实体 / 按 bot username 比对的 mention / 回复本 bot |
| 入站幂等键 | message_id | 合成 `evt:{Event}:{CreateTime}:{FromUser}` | MsgId | msgId | `update_id` |
| 传输形态 | webhook（3s 内 200）/ WS runner 列路线 | webhook（5s） | webhook（5s） | webhook（明文或加密）/ Stream runner 列路线 | webhook 或 **getUpdates 长轮询**（单活锁 + 游标） |
| 出站可达窗口 | 常规 | 常规 | 客服消息 48h | sessionWebhook ~2h | 常规 |

- **Telegram 群聊语义（权威口径）**：`getMe` 取回 bot username 缓存 1h（`bot_identity_cache`），
  据此做 @ 判定与指令归一化——`/help@mybot` 与 `@mybot /help` 都归一为 `/help`（否则群里
  指令实质不可用）；@ 别人不再误触发。冷缓存（拿不到 username）时**fail-open 回旧启发式**：
  宁多答勿失答。
- **额度与指令的次序**：身份指令（`/bind` `/unbind` `/help` `/status`）绕过 `allowed_chats`
  /`mention_only` 策略门——策略门是业务策略，不能成为用户的隐私出口（解绑）与管理动作的
  拦路石；`is_enabled=false` 对所有分支一视同仁（停用即全静默）。

### 观测与 SLO（最小指标平面已落地）

- **日桶计数器**（`yuxi/channels/telemetry.py`）：键 `channel:metric:{UTC日}:{app|all}:{指标}`，
  指标名是闭集 `Metrics`（入站量、IP 拦截、验签拒绝、陈旧、nonce 重复、内部错误、ingest
  失败、策略门拒绝/忽略、绑定成功/失败/熔断、派发、出站推送/改期/死信、保留期清理）；键带
  8 天 TTL 不无限增长，**写入一律 fail-open**（观测故障不得影响消息通路），只记计数与维度、
  不记正文。读取面 `GET /api/channels/metrics?days=1..8`（admin）返回 `by_day`/`by_app`，
  读失败时附 `errors` 而不是给一块静默的空看板。
- 死信可见性：`logger.error` + Redis 计数键 `channel:outbox:dead:count:{app_id}`
  + 管理页列表徽标（DEAD>0 红标）。告警接线（DEAD>0 立即告警、验签失败率
  >1%/5min、心跳缺失 >3min）由上述日桶读数判读；正式指标平面落地时把这些计数导出为
  counter/gauge，口径不变。
- 建议阈值：入站 p99 < 500ms；出站 p95 < 5s；死信率 < 0.1%（端点 `slo` 字段同源下发）。

## 后果

- 平台 SDK 依赖被隔离在 `adapters/`，切换传输形态（webhook ↔ 长连接）不动
  解析与推送层。
- 微信系验签的 timestamp/nonce 已进重放窗口；飞书加密回调的 headers 同理。
- token 缓存键含 credential 维度的版本化（F3）未做——当前键
  `channel:token:{type}:{platform_app_id}` 在「更新凭据」后最长残留 2h 旧
  token；P1 改为 `{app_id}:{credential_version}` 并在更新时主动 clear。
- 历史不再无限堆积：`purge_channel_history` 由 worker cron 每日 03:23 触发，按
  `CHANNEL_MESSAGE_RETENTION_DAYS`（默认 90）清理入站流水、终态 outbox 与过期绑定码；
  挂着 PENDING 分片的消息行**绝不删**（NOT EXISTS 护住——删流水会级联删掉未投递的 outbox），
  批量上限 2000 行/轮，清理量进 `retention_purged` 计数。

## 上线前最小验收

- 杀 channels 容器后重启：消息零丢失、零重复处理（游标 + 幂等双验证）。
- 重放同一回调 6 分钟后 → 403；窗口内同 nonce → 200 吞掉。
- 注入 DB 故障 → 200 + ERROR 日志，平台不重推。
- 平台并发重推同一消息 100 次 → 只落 1 行、只建 1 个 run。
- 分片中断第 3 片后重试 → 用户不看到重复前 2 片。
- 触发平台限流（429/45009）→ 退避/改期，不 DEAD、不放大。
- 群里 @ 别的 bot → 不回复；`/help@<bot>` 与 `@<bot> /help` 都能解析成 `/help`。
- `config.ip_allowlist` 切 `enforce` 且来源非平台出口 → 403，且 `inbound_ip_blocked`
  计数上升（先在 `log` 模式确认平台真实出口再切）。
- 钉钉加密模式 URL 校验：回体必须是 `{msg_signature,timeStamp,nonce,encrypt}` 密封包，
  明文回包钉钉不认。
- 同一用户绑定码连错 `CHANNEL_BIND_FAIL_LIMIT` 次 → 窗口内熔断；Redis 挂时不误伤正常用户。
- 保留期到期清理 → 有 PENDING 分片的消息行仍在（`retention_purged` 计数可核对）。
- 租户隔离：A 租户 token/消息/绑定对 B 不可见。
