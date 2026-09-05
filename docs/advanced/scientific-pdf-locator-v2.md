# 科研 PDF 定位与表格分块 V2

## 目标

科研 PDF 链路先建立可信的物理证据锚点，再由锚点生成检索 Chunk。页码、矩形、表格和引文定位不能从全文字符偏移猜测；无法可靠定位时必须保留正文检索能力，但关闭精确跳转。

## 解析器职责

- PyMuPDF 是 PDF 哈希、页数、CropBox/MediaBox、旋转和页面尺寸的物理权威。
- MinerU `content_list` 是正文、表格和图片语义块的主来源；其 0–1000 坐标必须按 PyMuPDF 页面几何转换为 PDF points。
- GROBID 提供题录、章节、参考文献、正文引文和 TEI 坐标，不用于覆盖已确认的物理页。
- `PhysicalPageMap` 按零基 `page_index` 汇集三种解析结果；UI 展示时才转换成一基页码。

## 证据契约

- 一个 `EvidenceAnchor` 可包含多个 `EvidenceFragment`，每个 fragment 保存 `page_index`、PDF points bbox、原文和来源 block id。
- Chunk 页码只聚合其锚点 fragment，不能由 Chunker 自行猜测。
- 低置信或越界坐标不得开放 PDF 高亮。
- `knowledge_chunks.source_provenance` 保存不可变科研来源，图谱抽取只写 `extraction_result`。
- 每个解析版本保存 `evidence-map.json`，用于从 Chunk 追溯 alignment、anchor 和三引擎物理页。

## 表格契约

- HTML 表格只在完整 `tr` 行边界拆分，每个分块重建完整 `<table>` 并重复表头。
- 连续跨页锚点整体附着到表体；锚点 ID 去重，物理页范围完整保留。
- PostgreSQL 保留 HTML 展示文本；Dense embedding 与 Milvus 检索上下文使用确定性的 `Table / Columns / Row` 自然语言表示。
- 禁止通用 token splitter 处理表格，禁止产生半截 `td>`、`tr>` 或不平衡标签。

## 能力与发布门禁

- `INDEXED_FULL` 要求 MinerU 与 GROBID 成功、高置信锚点覆盖率达标且不存在越界 MinerU 坐标。
- 解析升级可复用同源 SHA-256 的不可变 MinerU 原始产物，但 GROBID、坐标归一化、对齐、质量门禁和索引必须重跑。
- 较低能力的新解析不得替换当前活动索引。
- 同一解析版本每种 artifact role 只能存在一条记录。

## Golden PDF

回归样本 `10.1007/s11103-026-01722-w` 使用固定 SHA-256 与七个地标：Abstract p1、Introduction p1、Table 1 p3–4、转录调控章节 p7、Conclusions p9、References p10、Fig. 2 p10。任何解析器、对齐器或分块器升级都必须重新验证；表格 HTML 完整率和已知页准确率必须为 100%。
