# 渠道平台矩阵与接入手册

面向管理员/实施者的平台能力矩阵、接入前置条件与已知限制。能力实现以代码为准
（`backend/package/yuxi/channels/adapters/`），本文档随适配器演进同步更新。

## 一、平台能力矩阵

| | 飞书 | 企业微信 | 微信公众号 | 钉钉 | Telegram |
|---|---|---|---|---|---|
| **入站形态** | webhook（事件订阅 v2） | webhook（GET 校验 + 加密 XML） | webhook（80/443 + 备案域名） | webhook（**明文 / 加密 opt-in**） | webhook + **长轮询**（channels 容器） |
| **验签/加密** | encrypt_key（sha256 签名 + AES-256-CBC） | sha1 四元组 + AES-256-CBC + receiveid | 同企微（receiveid=appid） | HMAC 签名可选；**加密回调已支持**（signature + AES，challenge 加密回包） | secret_token 常量时间比对 |
| **来源 IP 白名单** | `config.ip_allowlist`（log/enforce） | 同左 | 同左 | 同左（**明文模式建议 enforce**） | 同左 |
| **幂等键** | message_id / event_id | MsgId + 事件合成键 | MsgId + 事件键 | msgId/createAt | update_id |
| **出站通道** | tenant_access_token + im/v1/messages | message/send（**需企业可信 IP**） | 客服消息 custom/send（**需认证服务号 + 48h 窗口**） | sessionWebhook（随消息下发，**~2h 时效**）；加密回调场景建议配 IP 白名单 | sendMessage（429 尊重 retry_after） |
| **出站 Markdown** | interactive 卡片（markdown 组件） | markdown 消息体 | 剥离为纯文本 | markdown 消息体 | 剥离为纯文本（无 parse_mode，避免转义坑） |
| **群聊** | ✅ 群内回复；@ 判定按 bot open_id 权威比对 | ⚠️ 群聊回复退化为应用消息私聊（见已知限制） | N/A（单聊） | ✅ sessionWebhook 群内回复 | ✅ 群内回复；@ 判定按 bot username（getMe 缓存）+ bot_command + 回复本 bot |
| **富媒体入站** | ❌ 统一提示「仅支持文本」 | ❌ | ❌ | ❌ | ❌ |
| **绑定入口** | 绑定码 | 绑定码 | 绑定码 + **带参二维码** | 绑定码 | 绑定码 |
| **长连接** | ❌ WS 未实现（见路线） | — | — | ❌ Stream 未实现（见路线） | ✅ getUpdates 长轮询（单活锁 + 游标持久化） |

Markdown 呈现闭集在 `render.markdown_mode` 登记：`native`（企微/钉钉）、
`card`（飞书）、`plain`（公众号/Telegram/未知平台）。新增平台必须在此登记。

## 二、接入前置条件（逐平台 checklist）

**全部平台共同**：
- [ ] 管理页新建后先处于 `DRAFT`；运行「连接测试」并消除全部阻塞项后，才可
      激活为 `ACTIVE`。编辑配置或轮换凭据会自动退回草稿并要求重测。
- [ ] webhook 型部署设置 `CHANNEL_PUBLIC_BASE_URL=https://bot.example.com`；该值
      必须是公网 HTTPS，不能使用浏览器地址、HTTP、localhost 或内网回环地址。
- [ ] 公网 HTTPS 入口（webhook 型）：APISIX 改绑公网 + TLS 证书；当前默认绑定
      `127.0.0.1:9088` 仅供本机调试。
- [ ] `YUXI_PUBLIC_WEB_URL` 配置为用户可访问的 Web 地址（决定回复中的
      「查看完整结果」链接；仅个人绑定用户会收到链接）。
- [ ] 管理页（/channel-manage）建应用 → 把返回的 webhook 地址（含一次性路径
      令牌）配到平台后台。
- [ ] 建议：`config.ip_allowlist_mode=log` 观察一周平台出口 IP 分布后切
      `enforce`。

**飞书**：
- [ ] 自建应用；事件订阅选 `im.message.receive_v1`；**开加密模式**
      （encrypt_key + verification_token 都配进凭据）。
- [ ] 订阅后**发布应用版本**（不发布收不到事件）。
- [ ] 权限：`im:message`（收）、`im:message:send_as_bot`（发）。
- 症状对照：收不到事件 → 未发布/回调地址不通；出站 403 → 凭据错；发送失败
  看 outbox 的 last_error。

**企业微信**：
- [ ] 自建应用五项凭据：corp_id / corp_secret / agent_id / 回调 token /
      EncodingAESKey（43 位）。
- [ ] **企业后台配置「企业可信 IP」**（Yuxi 出口 IP）——不配则所有出站 API
      报 60020，回复全 DEAD。
- 已知限制：群聊消息的回复走应用消息，表现为私聊（见第四节）。

**微信公众号**：
- [ ] **认证服务号** + 开通「客服消息」接口——未认证/未开通时能收不能回，
      出站全 DEAD（errcode 45009/48004 类）。
- [ ] 回调 URL 仅支持 80/443 且域名需 ICP 备案。
- [ ] 回复仅在用户 48h 内互动过的会话窗口内可送达；超窗 `undeliverable` DEAD。

**钉钉**：
- [ ] 企业内部机器人；凭据配 `encoding_aes_key` + `token` + `corp_id` 即启用
      **加密回调**（推荐；仅配 `app_secret` 则为明文 + 可选 HMAC 签名）。
- [ ] 明文模式上线前**必须**配 `ip_allowlist`（enforce）——明文回调无报文级
      加密，IP 白名单是主要防线。
- [ ] 出站依赖入站消息携带的 sessionWebhook（~2h 时效）；运行超过 2h 的结果
      会 `undeliverable` DEAD——属平台形态限制，超长任务请用 Web 端。

**Telegram**：
- [ ] BotFather 建 bot 取 token；webhook 模式需公网 HTTPS + 建议配
      `webhook_secret`；无公网环境用长轮询（`channels` 容器自动接管，单实例）。
- [ ] 长轮询模式：连接测试通过并激活后 ≤60s 自动开始拉取（动态 reconcile）；
      多副本部署由单活锁保证只有一个实例拉取。

## 三、指令与身份绑定

渠道内指令：`/bind <绑定码>` 绑定 Yuxi 账号、`/unbind` 解绑、`/reset` 开新
会话、`/status` 查看绑定状态、`/help` 帮助。身份指令**不受**策略门（白名单/
@ 提及）限制——未授权会话里 `/help` 也可用；普通消息照常过门。

绑定码：管理页生成（10 分钟一次性、只存哈希）；同应用同用户失败 5 次/10 分钟
熔断。绑定后消息以本人 Yuxi 账号执行（个人配额/BYOK 生效）；未绑定走渠道
服务账号（组织记账）。绑定用户被停用自动回退服务账号并留痕。

## 四、已知限制与路线

| 项 | 状态 | 说明 |
|---|---|---|
| 企微群内回复 | 限制 | 入站群聊的 chat_id 为发送者 userid，出站 `touser=` 该用户 → 群里问、私聊答。真群内回复需群机器人 webhook 或客户群接口（未实现）。管理页请按「私聊助手」口径向用户说明。 |
| 飞书 WS 长连接 | 未实现（路线） | 无公网入口的客户暂无法接入飞书；Telegram 长轮询已可用。 |
| 钉钉 Stream | 未实现（路线） | 同上；当前钉钉依赖公网 webhook。 |
| 富媒体入站 | 未实现 | 图片/文件/语音统一回「暂仅支持文本，可到 Web 端上传」。 |
| 卡片出站 | 飞书已支持 | 其余平台 markdown 消息体/纯文本；交互按钮未做。 |
| 指标告警接线 | 数据已落 Redis 日桶 | `GET /api/channels/metrics`（admin）可读聚合与 SLO 口径；告警通道（webhook/IM 通知）未接。 |
| 保留期 | 已实现 | cron 每日 03:23 清理超期流水/终态 outbox/过期绑定；未投递完的 PENDING 分片绝不删。 |

## 五、运维速查

- 死信：管理页列表 DEAD 徽标 → 明细页 requeue；`undeliverable:` 前缀 = 平台
  形态性不可达（sessionWebhook 过期/超客服窗口），requeue 前先确认用户重新
  互动过。
- 验签失败激增：`channel.inbound.signature_rejected` 日桶 → 有人扫描或平台
  密钥轮换未同步。
- Telegram 长轮询健康：容器 healthcheck 锚定 `/tmp/channels_heartbeat`
  （60s 刷新，缺失 >200s 判不健康）；监督循环状态变化有日志。
- 路径令牌泄漏：管理页「重置令牌」立即作废旧地址，平台侧同步改配。
- `.env` 修改 `CHANNEL_PUBLIC_BASE_URL` 后需重建相关容器使环境变量生效：
  `docker compose up -d --force-recreate api worker channels`。
