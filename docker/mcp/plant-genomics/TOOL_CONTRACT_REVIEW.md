# plant-genomics-mcp v1.21.0（commit ddd223f）逐工具契约审核

审核对象：`yuxi/agents/mcp/capability_registry.py` 中 `plant-genomics` 服务器的受信能力注册面。
原则：**只有"给定精确标识符/区间，返回上游数据库结构化源记录"的检索工具可注册能力**；
合成、聚合分析或语义不符的工具一律不注册——无 profile 的工具在 source-constrained
轮次（MCP_ONLY / 证据等级冻结）被 `knowledge_context` 过滤，永不满足来源义务，也不产生
可引用的 MCP-F 事实账本。

结论日期：2026-09-21。上游工具面以镜像内 `plant_genomics_mcp` 包为准（固定 commit，
构建时校验 `git rev-parse HEAD`）。

## 注册（GENE_RECORD_LOOKUP / STRUCTURED_DATABASE / CROSS_SOURCE_AGGREGATOR）

| 工具 | 上游数据源 | 审核结论 |
| --- | --- | --- |
| ensembl_plants_lookup_locus / batch_ | Ensembl Plants REST lookup | 精确 locus 检索，返回结构化记录 ✓ |
| get_gene_xrefs / batch_ | Ensembl Plants xrefs | 精确交叉引用检索 ✓ |
| get_sequence | Ensembl Plants sequence | 精确序列检索 ✓ |
| ensembl_region_query | Ensembl Plants region | 精确区间检索 ✓ |
| phytozome_lookup_locus / batch_ | Phytozome | 精确 locus 检索 ✓ |
| resolve_locus_to_uniprot / batch_ | Ensembl Plants → UniProt 映射 | 精确映射检索 ✓ |
| locus_go_annotations / batch_ | GO API | 精确注释检索 ✓ |
| locus_plant_ontology | Plant Ontology | 精确注释检索 ✓ |
| gramene_homologs / batch_ | Gramene homology | 精确同源检索 ✓ |
| kegg_pathways | KEGG REST | 精确通路检索 ✓ |
| bar_gene_summary | BAR eFP | 精确基因摘要 ✓ |
| bar_efp_expression | BAR eFP | 精确表达检索 ✓ |
| bar_aiv_interactions / batch_ | BAR AIV | 精确互作检索 ✓ |
| string_interactions / batch_ | STRING | 精确互作检索 ✓ |
| tair_locus_info | TAIR | 精确 locus 检索 ✓ |
| plantcyc_locus_info | PlantCyc | 精确通路/基因检索 ✓ |
| alphafold_structure | AlphaFold DB | 精确结构检索 ✓ |
| experimental_structures | PDB | 精确结构检索 ✓ |
| tf_binding_motifs | DAP-seq/TF 数据 | 精确 motif 检索 ✓ |
| jaspar_motif | JASPAR | 精确 motif 检索 ✓ |
| experimental_interactions | BioGRID/IntAct 类 | 精确互作检索 ✓ |
| locus_gene_rifs | NCBI Gene RIF | 精确注释检索 ✓ |
| interpro_domains | InterPro | 精确结构域检索 ✓ |
| locus_variants | Ensembl Plants 变异 | 精确变异检索 ✓ |
| vep_annotate | Ensembl VEP | 精确变异注释 ✓（输入输出均结构化） |
| panther_family | PANTHER | 精确家族检索 ✓ |
| orthodb_orthologs | OrthoDB | 精确同源检索 ✓ |
| aragwas_associations | AraGWAS | 精确关联检索 ✓ |
| arabidopsis_natural_variation | 1001 Genomes 等 | 精确变异检索 ✓ |
| atted_coexpression / batch_ | ATTED-II | 精确共表达检索 ✓ |

## 注册（BIBLIOGRAPHIC_SEARCH / BIBLIOGRAPHY）

| 工具 | 上游数据源 | 审核结论 |
| --- | --- | --- |
| locus_literature / batch_locus_literature | Europe PMC 等 | 题录检索，BIBLIOGRAPHIC_PROVENANCE 语义：只证明文献元数据存在，不证明正文命题 ✓ |

## 不注册（从注册表移除，2026-09-21）

| 工具 | 移除原因 |
| --- | --- |
| analyze_locus_synth | 合成/聚合工具：跨库拼接"基因档案"，输出为二手投影而非单一源记录；作为唯一来源满足 GENE_RECORD_LOOKUP 义务会虚增权威性 |
| find_homologs_synth | 同上：合成检索 |
| biological_context_synth | 同上：上下文合成 |
| consensus_homologs | 同上：多源"共识"聚合，无单一可追溯源行 |
| gene_report | 同上：报告生成器 |
| go_enrichment | 富集分析（统计推断），不是基因记录检索；语义与 GENE_RECORD_LOOKUP 不符 |
| blast_sequence | 序列相似性搜索（启发式），不是精确记录检索；结果非结构化源记录语义 |

## 变更纪律

- 新增注册必须逐工具走本表审核：确认"精确输入 → 上游数据库结构化记录"后才可登记能力；
- 上游 bump（含 commit 固定值变更）必须重新对表——工具面变化（新增/改名/语义漂移）先改本文件再改注册表；
- 注册表的 key 是协议原始工具名，不是模型可见别名。
