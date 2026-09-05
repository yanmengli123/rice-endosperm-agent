# RC-G3 Rice Scientific PDF E2E 与 RC-G4 Failure Recovery

## 结论

2026-09-04 在本机 Docker 完整环境中，使用
`C:\Users\32110\Desktop\rice` 的真实论文完成科研 PDF 证据库 V1 验收。
原始论文目录只读使用，PDF 未复制进 Git。

- RC-G3A Ingestion / Citation E2E：**PASS**
- RC-G3B Dense + BM25 Hybrid Retrieval：**PASS**
- RC-G4 Worker / GROBID / Idempotency / Shadow Index：**PARTIAL PASS**（真实 worker 中断、幂等与 shadow index 已通过；GROBID 故障注入已通过，物理停容器尚未执行）
- 真实扫描件降级：**NOT TESTED**（本批 5 篇候选均为 born-digital）

## 环境与样本

- Knowledge Base：`kb_sgm3mj317r`
- 正文/学术解析：PyMuPDF + MinerU Official + GROBID `0.9.1-full`
- Embedding：`siliconflow-cn:BAAI/bge-m3`
- Re-ranker：`siliconflow-cn:BAAI/bge-reranker-v2-m3`
- 分块：`academic_scientific_v2`，600-token 目标、900-token 硬上限、64-token 重叠、排除 References
- 存储：PostgreSQL + MinIO 私有 bucket + Milvus dense/BM25

| 样本 | 文件 | SHA256 | DOI | 页数 |
| --- | --- | --- | --- | ---: |
| Rice-A | `1-s2.0-S1369526613000368-main.pdf` | `750c6a184f83a7b0272e28f4e79f2c8139fe8395c6ff6a1b87d1d148cfe0066c` | `10.1016/j.pbi.2013.03.001` | 11 |
| Rice-B | `Plant Biotechnology Journal - 2024 - Liu - A novel transcription factor OsMYB73 affects grain size and chalkiness by.pdf` | `d9bd84347b8f7dedda796a7d07eb59f3a02ed8a8d806b8dd33546db26185a2ef` | `10.1111/pbi.14558` | 18 |
| Rice-C | `Aquaporins in developing rice grains.pdf` | `f5680dabd5c88b0c1ec22a1025dae15ef1cc8f30844f685dccb3834bf67bb8a0` | `10.1080/09168451.2015.1032882` | 9 |

## 数据链结果

| 文件 ID | 状态 | 锚点 | References | Citation mentions | Chunks | Tokens |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `file_88c900` | `INDEXED_FULL` | 239 | 73 | 80 | 22 | 6,062 |
| `file_3be896` | `INDEXED_FULL` | 170 | 69 | 77 | 127 | 18,029 |
| `file_46107b` | `INDEXED_FULL` | 80 | 46 | 64 | 41 | 6,294 |

三篇文献的 NATIVE、MINERU、GROBID、UNIFIED、QUALITY、INDEX、ACTIVATE 阶段均有持久状态。
全部 190 个活动 chunk 均具有结构化 `section_path`、`page_numbers`、`evidence_anchor_ids`、
`parse_revision_id` 和 `index_revision_id`。

Rice-A canonical 抽查：题名为
`Functional genomics based understanding of rice endosperm development`；作者为
Shi-Rong Zhou、Lin-Lin Yin、Hong-Wei Xue；共 15 sections；canonical SHA256 与原 PDF 一致。
Abstract、正文分节和 References 均存在，未观察到典型双栏交叉拼接。

三篇活动索引的 References section chunk 和纯编号 bibliography-like chunk 均为 0。
Rice-A 保留 19 个 paragraph、2 个 figure、1 个 table；Rice-B 保留 51 个 figure 和 29 个 caption；
Rice-C 保留 15 个 figure。图表内容和资源引用已保留，但部分图表尚不能确定性对齐页码/bbox。

### 私有图片预览：PASS

2026-09-05 使用 `s11103-026-01722-w.pdf` 补充真实预览验收：旧
`scientific_pdf_v1` 版本的 Markdown 直接引用私有 MinIO URL，浏览器请求被 403 拒绝；升级后的
`scientific_pdf_v1.1` 版本 `spr_4eed92b855252ee6ab02f31ccba0afb2afc1a9ae` 原子激活成功，
30 个学术 chunk / 8,716 tokens 已进入活动索引。

- canonical Markdown 含 3 个 `kbasset://` 图片引用，MinIO 直链为 0；
- 浏览器通过同源、Bearer 鉴权的知识 Asset API 取得 3 张图片，HTTP 均为 200；
- 渲染后的图片均为短生命周期 `blob:` URL，实测自然尺寸分别为 82×81、670×553、485×398；
- 未携带认证访问 Asset API 返回 401，直接访问私有 MinIO 对象返回 403；
- 服务端仅允许 JPEG/PNG/WebP/GIF，校验知识库、文件、租户、解析版本与对象前缀，并支持 ETag/304；
- 解析运行时的 `asset_uri_builder` 与私有 `image_prefix` 不写入 `processing_params`，避免 JSONB
  序列化失败和存储布局泄露。

## 真实 Hybrid Retrieval

问题：`What evidence supports the role of GIF1 in rice grain filling and endosperm development?`

| 检索层 | GIF1 命中 | 排名 | 分数 |
| --- | --- | ---: | ---: |
| Dense | Rice-A 正文 | 2 | 0.617380 |
| BM25 | Rice-A 正文 | 1 | 9.024960 |
| Weighted hybrid | Rice-A 正文 | 1 | 0.844995 |
| Hybrid + reranker | Table 1 / GIF1 | 1 | 0.719717 |
| Hybrid + reranker | Rice-A 正文 | 4 | 0.713889 |

正文证据可追溯至 `file_88c900...chunk_17`、章节 `Regulation of endosperm size`、
第 7 页、锚点 `ea_8b43230397ba7181f2bc308abd1339e281d2b5c4`、parse revision
`spr_4e7ea4775b95abd4918b2ae120a8992967ef7c65`、index revision
`sir_0beceaedfcfe3b3309cbbee4b9b0d8cfd6a03057` 和 Rice-A 源 SHA256。

真实请求中 dense API 成功；hybrid 首次连接发生一次 `ConnectError` 后重试成功；reranker
约 2.96 秒完成。没有用 BM25-only 代替 hybrid 验收。

## RC-G4

### Worker kill / lease recovery：PASS

Rice-B、Rice-C 处理期间停止 Worker，并将测试租约置为过期。新 Worker 启动后重新领取任务，
parse `attempt` 从 1 增至 2，复用已成功 stage，最终均为 `INDEXED_FULL`；恢复期间旧活动索引持续可用。

测试发现并修复 `TIMESTAMPTZ` 与 naive UTC 比较导致过期判断偏移 8 小时的问题；工作流现统一使用
aware UTC。

### GROBID down：PASS（模拟故障）

MinerU 成功、GROBID 连接失败时，自动测试确认文档降级为 `INDEXED_CONTENT_ONLY`，
`fulltext_search=true`、`citation_navigation=false`，GROBID stage 为 `DEGRADED` 且保留错误码。
真实 GROBID 容器停机演练未执行，标记 **NOT TESTED**。

### Idempotency / shadow index：PASS

- 相同租户、文件、SHA256 和 parser fingerprint 重复提交只产生一个 parse revision。
- 新索引完成前始终由 `active_index_revision_id` 读取旧索引。
- 真实 embedding 连接失败没有替换活动指针；成功重试后才原子激活。
- 迁移 `0019_scientific_pdf_single_active_index` 修正历史审计状态并建立部分唯一索引。
- 当前每个文件恰好一个 `ACTIVE` index revision，历史版本为 `SUPERSEDED`。

## Gate 汇总

| Gate | 状态 |
| --- | --- |
| Ingestion / lease / heartbeat | PASS |
| Native / MinerU / GROBID / canonical | PASS |
| Academic chunking | PASS |
| Dense / BM25 / fusion / reranker | PASS |
| Shadow indexing / publication | PASS |
| Retrieval / evidence / citation / provenance | PASS |
| Frontend refresh persistence | PASS |
| Figure/Table content preservation | PASS |
| Figure/Table page/bbox full coverage | NOT TESTED / PARTIAL |
| Real scanned/degraded PDF | NOT TESTED |
| Simulated GROBID degradation | PASS |

## 工程门禁

- Backend feature regression：114 passed
- Ruff（本功能相关 Python 文件）：PASS
- Web targeted ESLint：PASS
- Web production build：PASS（仅第三方 pure annotation 与 bundle-size warnings）
- `docker compose config --quiet`：PASS
- `git diff --check`：PASS
