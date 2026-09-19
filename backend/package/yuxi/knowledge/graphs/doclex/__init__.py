"""文档理解面（Stage B / doclex）：解析面之上、断言抽取面之下的解释层。

设计要点（对齐 ADR「解析面确定性」不变量）：

- 解析修订（parse_revision）是跨文件复用的物理事实，doclex 永不进入解析流水线；
- doclex 以 (file_id, fingerprint) 内容寻址幂等构建，指纹 = 算法版本 + 词典版本 +
  解析修订——词典升级 → 新指纹 → 新修订重算，与 spr_/sir_ 模式同构；
- 产物三类：分诊条目（entries）、定义断言（definitions）、注入词典（Pass 2 用）。
"""

from yuxi.knowledge.graphs.doclex.bracket_triage import (
    BRACKET_TRIAGE_VERSION,
    KIND_ABBREV_DEFINITION,
    KIND_ALIAS_TAXON,
    KIND_CONDITION_PARAMETER,
    KIND_EQUIPMENT_ATTRIBUTE,
    KIND_LOCAL_RULE,
    KIND_UNCERTAIN,
    BracketAnnotation,
    classify_brackets,
    taxon_alias_target,
)
from yuxi.knowledge.graphs.doclex.coref_dictionary import (
    COREF_DICTIONARY_VERSION,
    KIND_ABBREV,
    KIND_COREFERENCE,
    CoreferenceEntry,
    build_coreference_dictionary,
)
from yuxi.knowledge.graphs.doclex.temporal_definitions import (
    TEMPORAL_DEFINITION_VERSION,
    TemporalDefinition,
    parse_temporal_definitions,
)

__all__ = [
    "BRACKET_TRIAGE_VERSION",
    "BracketAnnotation",
    "COREF_DICTIONARY_VERSION",
    "CoreferenceEntry",
    "KIND_ABBREV",
    "KIND_ABBREV_DEFINITION",
    "KIND_ALIAS_TAXON",
    "KIND_CONDITION_PARAMETER",
    "KIND_COREFERENCE",
    "KIND_EQUIPMENT_ATTRIBUTE",
    "KIND_LOCAL_RULE",
    "KIND_UNCERTAIN",
    "TEMPORAL_DEFINITION_VERSION",
    "TemporalDefinition",
    "build_coreference_dictionary",
    "classify_brackets",
    "parse_temporal_definitions",
    "taxon_alias_target",
]
