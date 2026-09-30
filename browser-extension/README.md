# 语析本机浏览器连接(Chrome/Edge MV3 扩展)

把本机 Chrome/Edge 浏览器接入语析(Yuxi)服务端的 Browser Gateway:服务端通过 WebSocket 下发浏览命令,扩展在**任务窗口**内代为执行导航、读取、点击、输入与截图等操作。纯原生 JavaScript(service worker + ES modules),无构建步骤、无外部依赖;UI 全中文。

## 功能特性

- 一次性配对码配对,设备令牌(`brt_...`)本机加密存储区持久化,支持服务端轮换令牌(`token_rotated`)。
- WebSocket 长连接:`hello/hello_ok` 握手、按配对响应的 `heartbeat_interval_s`(默认 10s)发送心跳、45 秒无服务端帧判定死链并重连、指数退避(1s 起,上限 30s)。
- 任务窗口制安全模型:服务端命令只能操作任务窗口内的标签页,或用户在弹出页显式「借出」的标签页。
- 支持操作:`navigate`、`read_page`、`click`、`type`、`screenshot`、`get_status`、`end_task`。
- service worker 保活:WS 心跳本身维持存活,另以 0.5 分钟周期的 `chrome.alarms` 兜底校验连接健康度。

## 环境要求

- Chromium 内核 125 及以上(Chrome / Edge 均可)。
- 能访问语析服务端(配对链接中的地址)。

## 安装(开发者模式加载)

1. 打开 `chrome://extensions`(Edge 为 `edge://extensions`)。
2. 打开右上角「开发者模式」。
3. 点击「加载已解压的扩展程序」,选择本目录(`browser-extension/`)。
4. 工具栏出现蓝色圆形图标即安装成功。

## 配对步骤

1. 在语析服务端(网页或 API)为本设备生成**配对链接**,形如
   `http://<主机>[:<端口>]/api/browser/pairing?p=<一次性码>`。
2. 点击扩展图标,在弹出页点击「打开设置页」。
3. 把完整配对链接粘贴到「配对链接」输入框;设备名称默认自动生成(如 `Chrome on Windows`),可自行修改。
4. 点击「开始配对」。成功后设置页显示服务器、设备名称与令牌有效期,弹出页状态变为「已连接」。

配对接口:`POST {origin}/api/browser/extension/authorize`,一次性码即凭证,无需登录态。配对失败会显示服务端原因(链接无效 / 已过期 / 已被使用 / 用户不可用等)。

## 使用说明与安全边界

- **任务窗口**:服务端下发 `navigate` 时,扩展会自动创建一个独立的普通浏览器窗口作为「任务窗口」,后续命令默认只作用于其中的标签页。
- **借出标签**:如需让服务端操作任务窗口以外的页面(例如你已登录好的站点),在该标签页上点扩展图标,点击「借出当前标签给任务」;点「从任务收回该标签」解除标记(仅解除标记,不会关闭标签)。
- **登录 / 验证码**:需要人工完成的登录、图形验证码、扫码等请由用户自己在任务窗口内完成后,再让服务端继续执行命令。
- 任务结束(服务端下发 `end_task`,或用户手动关闭任务窗口)会关闭任务窗口并清空借出标记。
- 扩展仅与配对链接中的服务器通信(REST 配对 + WebSocket 命令通道),无其他网络请求;日志不输出设备令牌。
- 设备令牌支持服务端主动轮换(下发 `token_rotated` 帧后自动持久化并用新令牌重连)。

## 故障排查

### WebSocket 关闭码

| 关闭码 | 含义 | 扩展行为 |
| --- | --- | --- |
| 4401 | 未认证(令牌无效) | 按指数退避自动重连;若持续出现,请在设置页重新配对 |
| 4403 | 设备已被吊销或被其他设备替换 | **停止自动重连**,关闭任务窗口并清除本地凭证,需重新配对 |
| 4408 | 心跳超时 | 自动重连 |

### 命令错误码(服务端收到的 `error.code`)

| 错误码 | 含义 |
| --- | --- |
| `EXT_INVALID_URL` | navigate 的 url 不是 http(s) 链接 |
| `EXT_TAB_NOT_AUTHORIZED` | 目标标签不在任务窗口内且未借出 |
| `EXT_NO_TASK_WINDOW` | 尚无任务窗口(先下发 navigate 创建) |
| `EXT_PAGE_UNSUPPORTED` | 页面不支持注入/截图(如 `chrome://` 内置页) |
| `EXT_SELECTOR_NOT_FOUND` | 未找到目标元素(支持 CSS 选择器与 `text=`/`::-p-text=` 文本匹配兜底) |
| `EXT_ELEMENT_NOT_EDITABLE` | 定位到的元素不是可输入控件 |
| `EXT_SCREENSHOT_FAILED` / `EXT_SCREENSHOT_TOO_LARGE` | 截图失败 / 超过单帧 4MB 上限 |
| `EXT_BAD_REQUEST` | 命令 payload 缺字段或非法 |
| `EXT_UNKNOWN_OP` | 未知操作 |
| `EXT_INTERNAL_ERROR` | 扩展内部异常 |
| `EXT_RESULT_TOO_LARGE` | 结果超过单帧 4MB 上限 |

### 其他常见问题

- **一直「连接中…」或「离线」**:确认服务端地址可达、配对未过期;在设置页查看令牌有效期;必要时「断开并清除凭证」后重新配对。
- **命令一直无回应**:确认目标标签页不是 `chrome://` 等内置页;给浏览器内置页下发操作会返回 `EXT_PAGE_UNSUPPORTED`。
- **操作了错误的标签页**:任务窗口制下请确认目标标签在任务窗口内或已借出(弹出页可见任务窗口标签数与借出数)。
- **修改扩展代码后不生效**:在 `chrome://extensions` 点击该扩展的「重新加载」,并关闭重开任务窗口。

## 协议要点(与服务端 Browser Gateway 对齐)

- 配对:`POST /api/browser/extension/authorize`,请求 `{code, device_name, extension_version}`,响应含 `device_token / device_id / protocol_version / heartbeat_interval_s / token_expires_at`。
- 连接:`GET(WS) /api/browser/extension/ws`,MV3 无法自定义 Authorization 头,令牌经子协议 `["bearer", <token>]` 承载;首帧 `hello`,收到 `hello_ok` 前不发送其他帧;命令帧以同 `id` 回 `result` 帧。
- 单帧上限 4MB;协议版本 `v = 1`。

## 文件结构

```
browser-extension/
├── manifest.json      # MV3 清单(权限、入口、图标)
├── protocol.js        # 协议常量与帧构造/校验、配对链接解析
├── state.js           # 配对凭证(storage.local)与任务台账(storage.session)存取
├── ops.js             # 各命令实现与页面注入函数、错误码
├── background.js      # WS 客户端状态机、心跳/退避重连、命令分发
├── popup.html/css/js  # 弹出页:连接状态、借出/收回、设置入口
├── options.html/css/js# 设置页:配对、状态展示、断开并清除凭证
└── icons/             # 16/48/128 图标(纯色简单图形,程序生成)
```

版本 0.1.0 · 仅支持 Chromium 125+
