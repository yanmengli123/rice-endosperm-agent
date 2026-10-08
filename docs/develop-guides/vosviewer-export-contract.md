# VOSviewer 分析包导出契约

弦图 / 流向 / 矩阵消费同一 Neo4j 查询快照；VOSviewer 面板是同一快照的第四个
视图入口，导出的分析包是它的**外部分析投影**——无向实体关联网络 + 完整事实
明细，供本地 VOSviewer 1.6.21 做 bibliometric 风格分析。方向、谓词与调控语义
不进入连线；派生分析（布局/聚类）永不回写事实层或进入证据通道。

## 获取方式（唯一通道：签名快照凭证）

「图谱探索 → 洞察面板 → VOSviewer」视图内导出。画布查询（子图/全图）响应的
`meta.vosviewer_snapshot` 携带 HMAC 签名凭证（15 分钟、绑定 kb 与操作者、
sha256 指纹忽略数组顺序与请求时间）；导出 POST 凭证，服务端重新授权、按凭证内
query 重放同一查询并复核指纹，图谱已变化即 409 拒绝——防"点导出时图已变"的
时点错位，也杜绝无凭证旁路导出。

```sh
curl -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"kb_id":"<kb_id>","snapshot_token":"<meta.vosviewer_snapshot.token>","allow_candidates":false}' \
  "http://localhost:5050/api/graph/vosviewer/export" -o vosviewer.zip
```

- `allow_candidates=false`：正式分析（投影含候选关系时拒绝）；
- `allow_candidates=true`：探索用途，manifest 与摘要头标注 `exploration=true`。
- 凭证过期/不匹配 → 409/400，前端提示刷新图谱重新导出。
- 旧 GET `graph-export?variant=vosviewer` 已移除：快照必验，不允许旁路。

## 打开即地图

`vosviewer.json` 的每个 item 携带平铺 `x`/`y` 坐标（确定性种子布局）——
`VOSviewer.exe -json vosviewer.json` **打开即显示地图**（手册 5.1：`-json`
参数用于打开或创建地图；FileData 存在 openJSONFileToOpenMap 与
openJSONFileToCreateMap 两条路径，坐标即分野）。包内 `open-with-vosviewer.cmd`
双击即用：定位顺序为 `VOSVIEWER_PATH` 环境变量 → 本机默认安装路径 → 系统
PATH，然后以 `-json` 打开。种子布局仅是初始视图，可在查看器内重排另存；
算法与参数记录在 manifest `analysis` 节。

## 包成员

| 文件 | 内容 |
| --- | --- |
| `vosviewer.json` | 官方 JSON 契约：`network.items`（整数 id、label、`description` 单数字符串（HTML 实体已转义）、`weights` 命名对象（Links/Total link strength/Distinct facts）、平铺 `x`/`y` 种子坐标、`cluster` 连通分量编号）+ `network.links`（**source_id/target_id**/strength）+ `info` |
| `identity-map.json` | VOS 编号 ↔ entity_id/名称/类型；每条连线的事实 triple_ids 清单 |
| `facts.jsonl` | 每条去重事实：triple_id、端点（entity_id）、谓词、审核态、证据定位（kb_id+triple_id）、self_loop/resolved 标记 |
| `network.txt` | 传统 network 格式（source/target/strength 制表符分隔，同一快照生成） |
| `open-with-vosviewer.cmd` | CRLF 批处理启动器：定位 VOSviewer 并以 `-json` 打开（ASCII，避免代码页问题） |
| `manifest.json` | 快照范围、审核策略、UTC 时间、截断原因、全部计数与恒等式、排除明细、布局/聚类元数据、各成员 sha256、`DERIVED_ANALYSIS_ONLY` |
| `README.txt` | 打开方式、坐标口径与安全提示（Share 会上传公开） |

字段名证据链：桌面 1.6.21 `FileData.class` 常量池含 `source_id`/`target_id`/
"Link without SOURCE_ID."；官方示例 JSON（`*_network.json`、地图版）同为
`source_id`/`target_id`、`description` 单数、`weights` 命名对象、`x`/`y` 平铺。

**不导出 map.txt**：坐标的权威副本只在 vosviewer.json；另造一份 map 文件副本
只会与 JSON 漂移（历史上无坐标版本还会被 VOSviewer 拒绝：X and Y columns are
missing）。

## 语义契约

- **强度 = 不同 triple_id 数**：A→B 与 B→A 的去重事实合并为一根无向连线，
  strength 求和；chunk/来源片段数不参与计数。方向、谓词、审核态**只**在
  facts.jsonl，激活/抑制不得用正负权重表达。
- **身份**：entity_id / triple_id 为业务权威；VOS 整数编号由排序后 entity_id
  生成，仅在本包内有意义。
- **种子坐标**：`yuxi-seeded-force-v1`——黄金角螺旋初始化 + 网格桶斥力与弹簧
  引力固定步迭代，无随机源、同输入同输出（量化 1e-6、归一单位圆盘）；连通
  分量编号作为 `cluster`（纯结构事实，分量数 ≤1000 时导出，超限省略并在
  manifest 披露原因与真实分量数）。两者均可在查看器内重排替换，不构成事实。
- **排除与保留**：Chunk 结构节点与 MENTIONS 结构边排除并计数；实体自环不进
  links、单独计数（VOSviewer 不支持自环）；孤立实体保留为 items；端点缺失的
  事实剔除并记录 `missing_endpoint`。
- **审核门禁**：投影含 REJECTED 关系、同一 triple_id 身份不一致、并发更新中
  （`concurrent_update`）、恒等式不成立——均 fail-closed 拒绝出包。候选关系
  需显式 `allow_candidates=true`（且凭证口径为 candidates_visible）。
- **read-committed**：单条查询 + 端点封闭不提供全库某一时刻的版本快照承诺。

## 对账恒等式（fail-closed）

```
distinct_facts == total_link_strength + self_loop_facts + dangling_facts
items == entities == identity-map.items；坐标有限、唯一且 ∈ [-1,1]
network.txt 行数 == links；全部成员 sha256 与 manifest 一致
包内不含 map.txt；同快照两次构建字节一致（含坐标）
```

注意粒度：VOSviewer 是**实体级**网络，与三视图的**类型级**数字不可直接互比；
与三视图对账时比较的是输入事实集（同快照同策略下去重事实数一致）。

## 安全边界

- 导出要求管理员（get_admin_user）；每次导出写入图谱审计账本（action=
  VOSVIEWER_EXPORT，target_id=快照指纹）；权限撤销不追回已下载的本地副本。
- 私有图谱保持本地或企业内网分析：VOSviewer 内置 **Share 会上传地图并公开**，
  禁止对私有图谱使用（README 与本契约明示）。
- VOSviewer License 禁止修改本体——仅通过外部适配器与启动参数集成。
- 派生分析结果（坐标/聚类/密度）不写回 Neo4j、不进入证据通道。

## 验收记录（2026-10-08）

- 单元：13 用例（无向合并/结构排除/契约字段/布局确定性与有效性/分量省略/
  候选门禁/REJECTED fail-closed/并发阻断/快照签名与过期/指纹序不变性/zip
  成员与 sha256/启动器 CRLF+`-json`/无 map.txt）全部通过；图谱相关回归
  292 passed；ruff、ESLint、前端 7 spec、生产构建通过。
- 真实库 `kb_sgm3mj317r` 双口径程序化验收 28 项全过：candidates_visible
  3000 实体/3174 事实/2820 连线/900 分量（cluster 导出）；approved_only
  3000 实体全孤立（分量 3000 > 1000 → cluster 省略 + manifest 披露）。
  布局有效性实证：相连节点平均距离 0.058 vs 随机对 0.573。
- GUI 自动验收受会话隔离限制（agent 会话内启动的 VOSviewer 进程窗口不渲染，
  三轮截图实验证实）；字段契约经桌面二进制常量池与官方示例双重实证，
  `-json` 打开即地图由手册 5.1 与 FileData 双路径佐证，最终以用户桌面会话
  双击 `open-with-vosviewer.cmd` 验收为准。
