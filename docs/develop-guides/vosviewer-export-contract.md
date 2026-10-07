# VOSviewer 分析包导出契约

弦图 / 流向 / 矩阵消费同一 Neo4j 查询快照；VOSviewer 分析包是同一快照的**外部
分析投影**——无向实体关联网络 + 完整事实明细，供本地 VOSviewer 1.6.21 做
 bibliometric 风格分析。它不是第四视图：方向、谓词与调控语义不进入连线；
派生分析（布局/聚类）永不回写事实层或进入证据通道。

## 获取方式

管理员在「图谱探索 → 导出」菜单选择：

- **VOSviewer 分析包（仅已审核）**：`review_policy=approved_only`，正式分析默认；
- **VOSviewer 分析包（含 AI 候选，探索用）**：`candidates_visible`，包内
  manifest 标注 `exploration=true`，摘要头带 `exploration=true`。

```sh
curl -H "Authorization: Bearer <token>" \
  "http://localhost:5050/api/knowledge/databases/<kb_id>/graph-export?variant=vosviewer&review_policy=approved_only" \
  -o vosviewer-<kb_id>.zip
```

解压后在 VOSviewer `File → Open` 打开 `vosviewer.json`（无坐标时在 Create 中
基于网络创建地图）。

## 包成员

| 文件 | 内容 |
| --- | --- |
| `vosviewer.json` | 官方 JSON 契约：`network.items`（整数 id、label、descriptions[实体类型+entity_id]、weights[Links/Total link strength/Distinct facts]）+ `network.links`（source/target/strength）+ `info`。不预置 x/y/cluster——布局与聚类留给 VOSviewer |
| `identity-map.json` | VOS 编号 ↔ entity_id/名称/类型/度数；每条连线的事实 triple_ids 清单 |
| `facts.jsonl` | 每条去重事实：triple_id、端点（entity_id）、谓词、审核态、代表 chunk/file 定位、self_loop/resolved 标记 |
| `map.txt` / `network.txt` | 传统制表符格式（同一快照生成；map 字段已清洗制表/换行） |
| `manifest.json` | 快照范围、审核策略、UTC 时间、截断原因、全部计数与恒等式、排除明细、各成员 sha256、`DERIVED_ANALYSIS_ONLY` |
| `README.txt` | 使用说明与安全提示（Share 会上传公开） |

## 语义契约

- **强度 = 不同 triple_id 数**：A→B 与 B→A 的去重事实合并为一根无向连线，
  strength 求和；chunk/来源片段数不参与计数。方向、谓词、审核态**只**在
  facts.jsonl，激活/抑制不得用正负权重表达。
- **身份**：entity_id / triple_id 为业务权威；VOS 整数编号由排序后 entity_id
  生成，仅在本包内有意义。
- **排除与保留**：Chunk 结构节点与 MENTIONS 结构边排除并计数；实体自环不进
  links、单独计数（VOSviewer 不支持自环）；孤立实体保留为 items 与坐标轴候选；
  端点缺失的事实剔除并记录 `missing_endpoint`。
- **审核门禁**：投影含 REJECTED 关系、同一 triple_id 身份不一致、并发更新中
  （`concurrent_update`）、恒等式不成立——均 fail-closed 拒绝出包。候选关系
  需显式 `candidates_visible`。
- **快照凭证（可选）**：`issue_snapshot` 签发 HMAC 凭证（15 分钟、绑定 kb 与
  操作者、sha256 指纹忽略数组顺序与请求时间）；导出携带凭证时复验图谱未变化，
  变化即拒绝——防止"点导出时图已变"的时点错位。
- **read-committed**：单条查询 + 端点封闭不提供全库某一时刻的版本快照承诺。

## 对账恒等式（fail-closed）

```
distinct_facts == total_link_strength + self_loop_facts + dangling_facts
items == entities == identity-map.items
links == network.txt 行数；map.txt 行数 == entities + 1
全部成员 sha256 与 manifest 一致
```

注意粒度：VOSviewer 是**实体级**网络，与三视图的**类型级**数字不可直接互比；
与三视图对账时比较的是输入事实集（同快照同策略下去重事实数一致）。

## 安全边界

- 导出要求管理员（沿用既有 graph-export 鉴权）；权限撤销不追回已下载的本地副本。
- 私有图谱保持本地或企业内网分析：VOSviewer 内置 **Share 会上传地图并公开**，
  禁止对私有图谱使用（README 与本契约明示）。
- VOSviewer License 禁止修改本体——仅通过外部适配器与启动参数集成。
- 派生分析结果（坐标/聚类/密度）不写回 Neo4j、不进入证据通道。
