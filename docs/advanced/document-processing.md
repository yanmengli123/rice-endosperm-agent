# 文档处理与 OCR

Yuxi 将上传文件先保存为原文件，再解析为 Markdown 并按知识库分块策略入库。OCR 引擎既可在系统设置中作为默认值配置，也可在知识库上传或临时附件解析时逐次选择；未显式选择时使用 `default_ocr_engine`。

## 支持的文件类型

### 常规文档

| 类型 | 格式 | 说明 |
|------|------|------|
| 文本 | .txt, .md, .html, .htm | 直接提取内容 |
| Word | .docx | 保留格式和结构 |
| PowerPoint | .pptx | 保留主要文本结构 |
| PDF | .pdf | 支持文本和图片 PDF |
| 表格 | .csv, .xls, .xlsx | 识别表格结构 |
| JSON | .json | 结构化数据 |

### 图片文件

图片文件必须选择 OCR 引擎才能提取文字：
- .jpg, .jpeg, .png, .bmp, .tiff, .tif

### 压缩包

支持上传 ZIP 压缩包，系统会：
- 自动提取并处理其中的 Markdown 文件
- 处理图片并上传到对象存储
- 智能识别 `full.md` 或第一个 `.md` 文件

### 网页内容

知识库支持先从 URL 抓取页面内容，再作为文件进入现有上传、解析与入库链路：

1. 配置 `YUXI_URL_WHITELIST` 环境变量启用白名单机制
2. 系统自动将 HTML 转换为 Markdown
3. 内置去重机制，避免重复抓取

::: tip URL 白名单配置
示例：`YUXI_URL_WHITELIST=github.com,*.wikipedia.org,docs.python.org`
:::

## 本机文件与文件夹完整入库

在知识库文件管理中点击“添加文件”，可选择“本机文件”或“本机文件夹”：

1. “本机文件”支持一次选择一篇或多篇文献。
2. “本机文件夹”会递归上传所选目录中的受支持文件，并保留目录内相对路径。
3. “上传后自动入库”默认开启，任务会依次执行上传、Markdown 解析、分块和向量入库；切换页面不会中断后台任务。
4. 上传队列结束后点击“上传、解析并入库”，可在任务中心查看提交数、解析数、入库数和失败明细。

浏览器不会向网站暴露 `D:\...`、`C:\...` 等本机绝对路径，也不能通过文本框静默读取任意本地文件。用户必须使用系统文件或文件夹选择器主动授权；服务端只保存文件内容、文件名和所选目录内的相对路径。单文件仍受 100 MB 大小限制，重复内容会被知识库去重检查拒绝。

PDF 使用云端 OCR 时，上传、轮询和结果下载会对连接中断、超时、限流及 5xx 响应执行有限次数退避重试。如果 OCR 最终仍不可用，但 PDF 自带不少于 80 个非空白字符的文本层，系统会自动回退到本地文本提取并继续分块入库；扫描版 PDF 没有可用文本层时仍会明确失败，避免把空内容误标为已入库。可在处理参数中设置 `ocr_fallback_to_text: false` 关闭该回退。

## OCR 方案选择

系统提供多种 OCR 方案，适用于不同场景：

### 方案对比

| 方案 | 适用场景 | 硬件要求 | 特点 |
|------|----------|----------|------|
| RapidOCR | 基础文字识别 | CPU | 免费开源，速度快 |
| MinerU 本地服务 | 复杂 PDF、表格 | GPU | 自托管，版面分析能力强 |
| MinerU 官方 API | 复杂文档 | 无 | 官方精准解析 API，支持免费额度 |
| PP-Structure-V3 | 表格、票据 | GPU | 专业版面解析 |
| DeepSeek OCR | 智能理解 | 无 | 云端服务，Markdown 输出 |
| PaddleOCR-VL-1.6 | 复杂文档、表格、图片 PDF | 无 | 百度 AI Studio 云端服务，输出 Markdown |
| PP-OCRv6 | 基础文字识别 | 无 | 百度 AI Studio 云端 OCR，输出纯文本 |

后端保存的引擎标识与界面名称对应如下：`rapid_ocr`、`mineru_ocr`、`mineru_official`、`pp_structure_v3_ocr`、`deepseek_ocr`、`paddleocr_vl_1_6`、`paddleocr_pp_ocrv6`。PDF 也可以选择 `disable`，此时仅使用文本提取，不调用 OCR。

### 选择建议

- **个人使用或 CPU 环境**：选择 RapidOCR，免费且资源占用低
- **高精度需求**：选择 MinerU（需要 GPU）或 MinerU Official
- **表格密集型文档**：选择 PP-Structure-V3
- **云端版面解析**：选择 PaddleOCR-VL-1.6，适合希望输出 Markdown 的 PDF 或图片文档
- **云端纯文字识别**：选择 PP-OCRv6，适合只需要提取图片文字的场景
- **简单云服务**：选择 DeepSeek OCR 或 PaddleOCR API

## 快速配置

### RapidOCR

启动后会默认下载，无需配置

### MinerU（高精度）

项目已内置 mineru-api 服务（位于 docker-compose.yml，属于 all profile），无需额外下载官方 compose 文件。首次构建镜像时会基于 docker/mineru.Dockerfile 下载模型，该过程耗时较长。

启动服务（需要 GPU）：

```bash
docker compose --profile all up -d --build mineru-api
```

该服务在 `30001` 端口提供 `/file_parse` 接口，后端 `api` / `worker` 默认通过 `MINERU_API_URI=http://mineru-api:30001` 连接，通常无需额外配置。

::: tip 显存不足
若显存有限导致启动失败，可在 `docker-compose.yml` 的 `mineru-api` 服务下放开 `--gpu-memory-utilization` 参数（如 `0.5`，必要时进一步降低）。
:::

### MinerU 官方 API（云服务）

1. 登录 [MinerU Token 管理页面](https://mineru.net/apiManage/token) 创建并复制 Token。
2. 使用超级管理员账号进入“设置 → 基本设置 → 默认项配置”。
3. 在“MinerU 官方 API（免费额度）”卡片粘贴 Token，选择 `VLM（推荐）` 或 `Pipeline`。
4. 点击“保存并设为默认”。系统会先执行不创建解析任务的鉴权测试，通过后才保存，并将 `mineru_official` 设为全局默认 OCR 引擎。

Token 只保存在服务端，管理接口和页面不会回显明文。API 与后台任务通过同一份运行时缓存读取配置，因此无需在每个容器重复配置。官方精准解析 API 的认证、文件格式与异步解析流程以 [MinerU API 文档](https://mineru.net/apiManage/docs) 为准。

升级场景如需自动导入旧配置，可在首次启动新版本前临时配置 `MINERU_API_TOKEN`（兼容旧名 `MINERU_API_KEY`）。环境变量只在数据库尚未创建 MinerU 配置记录时导入一次，后续以设置页面保存值为唯一运行来源。

### PP-Structure-V3（结构化）

启动服务（需要 GPU）

```bash
docker compose up paddlex -d
```

### DeepSeek OCR（简单云服务）

在 .env 配置（使用已有的 SiliconFlow 密钥）

```env
SILICONFLOW_API_KEY=your-api-key-here
```

### PaddleOCR API（百度 AI Studio 云服务）

PaddleOCR API 使用百度 AI Studio 的 Access Token。获取方式：

1. 登录 [百度 AI Studio Access Token 页面](https://aistudio.baidu.com/account/accessToken)
2. 在页面中复制 Access Token
3. 在 `.env` 中配置为 `PADDLEOCR_API_TOKEN`

```env
PADDLEOCR_API_TOKEN=your-access-token-here
```

如需使用自定义 PaddleOCR API 地址，可额外配置：

```env
PADDLEOCR_API_URL=https://paddleocr.aistudio-app.com/api/v2/ocr/jobs
```

配置完成后，重启后端服务，在上传文件或解析临时附件时可以选择：

- `PaddleOCR-VL-1.6`：对应 `paddleocr_vl_1_6`，用于文档版面解析，返回 Markdown
- `PP-OCRv6`：对应 `paddleocr_pp_ocrv6`，用于基础 OCR，返回按行拼接的纯文本

## 解析参数与分块快照

知识库分块配置由两部分组成：`chunk_preset_id` 只表示策略（`general`、`qa`、`book`、`laws`、`semantic`、`separator`），具体参数统一放在 `chunk_parser_config` 中。不要再写入旧的根级 `chunk_size`、`chunk_overlap` 或 `qa_separator` 字段。

文件级 `processing_params` 会同时保存 `ocr_engine`、`ocr_engine_config`、分块策略和 `chunk_parser_config`。重新解析或入库时，系统以文件记录、知识库配置和本次请求合并后的快照为准，便于复现历史处理结果。

## 图片显示配置

上传文档中的图片需要正确配置才能在外部显示：

在 `.env` 中设置服务器 IP：

```
HOST_IP=your_server_ip
```

## 注意事项

1. **图片文件必须启用 OCR**：否则无法提取内容
2. **GPU 要求**：MinerU 和 PP-Structure-V3 需要 GPU 支持
3. **API 密钥**：MinerU Official、DeepSeek OCR、PaddleOCR API 等云服务需要额外的 API 密钥或 Access Token 配置
4. **超时处理**：复杂文档解析可能耗时较长，可通过 `MINERU_TIMEOUT` 环境变量调整超时时间
5. **文件大小限制**：知识库与工作区的单个上传文件大小均不超过 100 MB；工作区一次最多上传 50 个文件
6. **解析参数**：文件会保存当次 `ocr_engine`、`ocr_engine_config` 与分块参数快照。后续修改系统默认 OCR 或分块预设不会改写已上传文件的处理记录
7. **Agent 读取非文本文件**：Agent 的 `read_file` 只直接读取 UTF-8 文本和图片；遇到 PDF、Office 或其他二进制文件时，应使用 `ocr_parse_file` 生成 Markdown 后再读取
