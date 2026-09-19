"""科研图谱词法层：确定性实体预标注（零 LLM、零幻觉）。

为 ``llm_scientific`` 抽取器提供两件事：

1. **标识符实体的唯一合法来源**（G3 门禁）：RAP/MSU 基因编号只承认
   词法命中，LLM 不得生成标识符——对齐 ARCHITECTURE.md 架构不变量
   「LLM 不得生成引用标识符」；
2. **领域词典候选**：激素/品种/方法/组织/发育时期/处理条件在闭集
   类型下给出确定性命中，作为 LLM 链接 canonical 名称的候选。

标识符正则与 ``knowledge/evidence/span_builder.py`` 保持同族
（Os 前缀形态 + LOC_Os + RAP 数字形态），DOI/PMID 同引文模式。
词典是种子集：托管导入实体与别名表可经调用方注入扩展（见
``merge_lexicon_terms``），抽取收割的别名可回流变厚。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

LEXICON_VERSION = "rice-scientific-v2"

# ── 标识符（kind="identifier"）──────────────────────────────────
# RAP: Os03g0642100（Os + 2位染色体 + g + 7位）；MSU: LOC_Os03g64210（g 后 5-7 位宽容）
_RAP_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9_])Os\d{2}g\d{7}(?![0-9])")
_MSU_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9_])LOC_Os\d{2}g\d{5,7}(?![0-9])")
# OsNF-YB1 / OsbZIP 等命名基因 symbol：Os 前缀 + 大写/混合词（与 span_builder 同族，仅提示不做权威）
_OS_SYMBOL_PATTERN = re.compile(r"(?<![A-Za-z0-9_])Os[A-Z][A-Za-z0-9]{1,}-?[A-Za-z0-9]*\b")
_DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^\s;，。)\]】]+", re.IGNORECASE)
_PMID_PATTERN = re.compile(r"(?<![0-9])PMID[:：]?\s*(\d{6,9})(?![0-9])", re.IGNORECASE)

# 标识符 → 实体 label 映射；doi/pmid 不产实体（引文凭证，仅供 G3 拒绝伪造）
IDENTIFIER_LABELS: dict[str, str | None] = {
    "rap_id": "Gene",
    "msu_id": "Gene",
    "os_symbol": "Gene",
    "doi": None,
    "pmid": None,
}

# ── 领域词典（kind="dictionary"，label 为闭集类型）───────────────
_HORMONES = {
    "aba": ("ABA", "abscisic acid", "脱落酸"),
    "ga": ("GA", "GA3", "GA4", "gibberellin", "gibberellins", "gibberellic acid", "赤霉素"),
    "iaa": ("auxin", "IAA", "indole-3-acetic acid", "生长素"),
    "ja": ("jasmonic acid", "jasmonate", "JA", "茉莉酸", "茉莉酸甲酯", "MeJA"),
    "eth": ("ethylene", "ETH", "乙烯"),
    "br": ("brassinosteroid", "brassinolide", "BR", "BL", "油菜素内酯", "油菜素甾醇"),
    "ck": ("cytokinin", "CK", "细胞分裂素"),
    "sl": ("strigolactone", "SL", "独脚金内酯"),
}
_CULTIVARS = {
    "nipponbare": ("Nipponbare", "日本晴"),
    "9311": ("9311",),
    "zh11": ("Zhonghua 11", "ZH11", "中花11", "中花11号"),
    "kasalath": ("Kasalath",),
    "ir64": ("IR64",),
    "ir36": ("IR36",),
    "dular": ("Dular",),
    "mh63": ("Minghui 63", "MH63", "明恢63"),
    "zs97": ("Zhenshan 97", "ZS97", "珍汕97"),
}
_METHODS = {
    "qrt_pcr": ("qRT-PCR", "RT-qPCR", "quantitative real-time PCR", "实时荧光定量PCR"),
    "rt_pcr": ("RT-PCR", "semi-quantitative RT-PCR"),
    "qpcr": ("qPCR",),
    "rnaseq": ("RNA-seq", "RNA sequencing", "转录组测序"),
    "crispr": ("CRISPR/Cas9", "CRISPR-Cas9", "CRISPR", "基因编辑"),
    "rnai": ("RNAi", "RNA interference", "RNA干扰"),
    "overexpression": ("overexpression", "over-expressed", "OE", "过表达"),
    "knockout": ("knockout", "knock-out", "loss-of-function", "敲除", "敲除突变体"),
    "knockdown": ("knockdown", "knock-down", "敲低"),
    "y2h": ("yeast two-hybrid", "Y2H", "酵母双杂交"),
    "y1h": ("yeast one-hybrid", "Y1H", "酵母单杂交"),
    "luc": ("luciferase assay", "firefly luciferase", "LUC", "荧光素酶活性"),
    "emsa": ("EMSA", "electrophoretic mobility shift assay", "凝胶迁移阻滞"),
    "chip": ("ChIP-qPCR", "ChIP-seq", "ChIP", "染色质免疫共沉淀"),
    "dapseq": ("DAP-seq", "DNA affinity purification sequencing"),
    "bifc": ("BiFC", "bimolecular fluorescence complementation", "双分子荧光互补"),
    "coip": ("Co-IP", "co-immunoprecipitation", "免疫共沉淀"),
    "pulldown": ("pull-down assay", "pull-down", "下拉实验"),
    "gus": ("GUS staining", "GUS", "GUS染色"),
    "western": ("western blot", "immunoblot", "western blotting", "免疫印迹", "蛋白质印迹"),
    "subcellular": ("subcellular localization", "亚细胞定位"),
    "transient": ("transient expression", "瞬时表达"),
    "protoplast": ("protoplast transfection", "protoplast", "原生质体转化"),
}
_TISSUES = {
    "endosperm": ("endosperm", "胚乳"),
    "aleurone": ("aleurone layer", "aleurone", "糊粉层"),
    "embryo": ("embryo", "胚"),
    "seed": ("seed", "seeds", "种子"),
    "grain": ("grain", "grains", "caryopsis", "籽粒", "谷粒", "颖果"),
    "panicle": ("panicle", "panicles", "穗", "稻穗"),
    "spikelet": ("spikelet", "spikelets", "小穗"),
    "anther": ("anther", "花药"),
    "pollen": ("pollen", "花粉"),
    "root": ("root", "roots", "根"),
    "leaf": ("leaf", "leaves", "叶", "叶片"),
    "stem": ("stem", "culm", "茎", "秆"),
    "shoot": ("shoot", "苗"),
    "callus": ("callus", "愈伤组织"),
}
_CONDITIONS = {
    "drought": ("drought stress", "drought", "干旱胁迫", "干旱"),
    "salt": ("salt stress", "salinity stress", "salt", "盐胁迫"),
    "heat": (
        "heat stress",
        "heat",
        "high-temperature treatment",
        "high temperature",
        "HT treatment",
        "HT",
        "高温胁迫",
        "高温",
        "高温处理",
    ),
    "cold": ("cold stress", "chilling", "cold", "低温胁迫", "冷胁迫"),
    "oxidative": ("oxidative stress", "氧化胁迫"),
    "nitrogen": ("nitrogen deficiency", "low nitrogen", "氮缺乏", "低氮"),
    "phosphorus": ("phosphorus deficiency", "low phosphorus", "低磷"),
    # 常温对照（与高温处理配对出现；CT 作为独立缩写有 computed tomography 歧义，
    # 但水稻闭集域内按词边界命中是安全的，检索期歧义由实体解析层兜底）
    "control_temperature": (
        "control temperature",
        "control temperature treatment",
        "CT treatment",
        "CT",
        "常温对照",
        "常温处理",
        "常温",
    ),
}
# 发育期中的固定词；"N DAP/DAF" 模式单独匹配
_STAGES = {
    "grain_filling": ("grain filling stage", "grain filling", "filling stage", "灌浆期", "灌浆"),
    "heading": ("heading stage", "heading date", "抽穗期"),
    "tillering": ("tillering stage", "tillering", "分蘖期"),
    "seedling": ("seedling stage", "seedling", "苗期"),
    "milk_ripe": ("milk-ripe stage", "milk ripe stage", "乳熟期"),
    "dough": ("dough stage", "成熟期", "蜡熟期"),
    "flowering": ("flowering", "anthesis", "开花期", "抽穗开花"),
    # 灌浆分期：定义性断言的宿主词条（区间定义在 doclex 层解析，词典只负责识名）
    "grain_filling_early": ("early grain filling stage", "灌浆前期", "灌浆初期"),
    "grain_filling_middle": ("middle grain filling stage", "mid grain filling", "灌浆中期"),
    "grain_filling_late": ("late grain filling stage", "灌浆后期"),
}

# 抽穗后天数缩写全称（DAH 系）：作为 DevelopmentStage 词典词条预标注，
# 时间区间的结构化解析在 doclex.temporal_definitions 完成
_HEADING_DAYS_TERMS = ("days after heading", "DAH", "dDAH", "抽穗后天数", "抽穗后")
_FLOWERING_DAYS_TERMS = ("days after flowering", "DAF", "dDAF", "开花后天数", "开花后")

DICTIONARY_LEXICONS: dict[str, dict[str, tuple[str, ...]]] = {
    "Condition": {**_HORMONES, **_CONDITIONS},
    "Cultivar": _CULTIVARS,
    "Method": _METHODS,
    "Tissue": _TISSUES,
    "DevelopmentStage": {
        **_STAGES,
        "days_after_heading": _HEADING_DAYS_TERMS,
        "days_after_flowering": _FLOWERING_DAYS_TERMS,
    },
    # 鲜重/干重等度量：按可观测性状处理（闭集无独立 Measure 类型）
    "Phenotype": {
        "fresh_weight": ("fresh weight", "FW", "鲜重"),
        "dry_weight": ("dry weight", "DW", "干重"),
    },
}

# DAP/DAF 数值发育期："10 DAP"、"10 days after pollination" 的确定性锚定
_DAP_PATTERN = re.compile(r"(?<![0-9])(\d{1,3})\s*(?:DAP|DAF|DDP|DAH|dDAP|dDAF|dDAH)\b")
_DAP_LONG_PATTERN = re.compile(
    r"(?<![0-9])(\d{1,3})\s*(?:days?|d)\s+after\s+(?:pollination|fertilization|flowering|heading)", re.IGNORECASE
)

# 闭集实体类型：对齐 managed_import_parser.NODE_TYPE_MAPPING 的内部 label（Gene 三态合一）
# v2（R7c）：新增 Observation（表格观测记录）——由解析面确定性投影产生，
# LLM 抽取默认不产此类型（prompt 指南限定表格行语义）
SCIENTIFIC_ENTITY_TYPES: tuple[str, ...] = (
    "Gene",
    "AlleleMutant",
    "Phenotype",
    "Process",
    "QTL",
    "CisElement",
    "RNA",
    "Protein",
    "Pathway",
    "Tissue",
    "DevelopmentStage",
    "Condition",
    "Cultivar",
    "Experiment",
    "Publication",
    "Method",
    "Observation",
)


@dataclass(frozen=True)
class LexiconMatch:
    """一次词法命中。identifier 类的 value 是标识符本身（G3 比对用）。"""

    surface: str
    label: str | None
    start: int
    end: int
    kind: str  # "identifier" | "dictionary"
    value: str | None = None


_compiled_dictionary_cache: dict[str, Any] = {"revision": -1, "latin": [], "cjk": []}
_dictionary_revision = 0


def merge_lexicon_terms(label: str, terms: dict[str, tuple[str, ...]]) -> set[str]:
    """注入外部词典种子（如托管导入实体/别名表），运行时扩充闭集词典。

    返回本次实际新增的键集合（已存在的键被覆盖不计入）——调用方构建结束后用
    :func:`remove_lexicon_terms` 回滚，保证「KB 作用域装载、进程内不残留」。
    内容变化会改变 :func:`lexicon_content_digest`，进而失效抽取缓存（R2a）。
    """
    global _dictionary_revision
    target = DICTIONARY_LEXICONS.setdefault(label, {})
    added = {key for key in terms if key not in target}
    target.update(terms)
    _dictionary_revision += 1
    return added


def remove_lexicon_terms(label: str, keys: set[str]) -> None:
    """回滚 merge_lexicon_terms 的注入（KB 作用域词典装载的 finally 清理）。"""
    if not keys:
        return
    global _dictionary_revision
    target = DICTIONARY_LEXICONS.get(label)
    if target is None:
        return
    for key in keys:
        target.pop(key, None)
    _dictionary_revision += 1


def _compiled_dictionary() -> tuple[list[tuple[re.Pattern, str]], list[tuple[str, str]]]:
    """按词典修订号缓存编译结果：拉丁词条编译为词边界正则，中文词条保留子串查找。"""
    if _compiled_dictionary_cache["revision"] == _dictionary_revision:
        return _compiled_dictionary_cache["latin"], _compiled_dictionary_cache["cjk"]
    latin: list[tuple[re.Pattern, str]] = []
    cjk: list[tuple[str, str]] = []
    for label, groups in DICTIONARY_LEXICONS.items():
        for terms in groups.values():
            for term in terms:
                term = term.strip()
                if not term:
                    continue
                if re.search(r"[A-Za-z]", term):
                    latin.append(
                        (re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", re.IGNORECASE), label)
                    )
                else:
                    cjk.append((term, label))
    _compiled_dictionary_cache.update({"revision": _dictionary_revision, "latin": latin, "cjk": cjk})
    return latin, cjk


def extract_identifiers(text: str) -> list[LexiconMatch]:
    """标识符命中：按出现顺序去重（同值只保留首现）。"""
    matches: list[LexiconMatch] = []

    def push(kind: str, match: re.Match, group: int = 0) -> None:
        surface = match.group(group) if group else match.group(0)
        matches.append(
            LexiconMatch(
                surface=surface,
                label=IDENTIFIER_LABELS.get(kind),
                start=match.start(group) if group else match.start(),
                end=match.end(group) if group else match.end(),
                kind="identifier",
                value=surface,
            )
        )

    for match in _MSU_ID_PATTERN.finditer(text):
        push("msu_id", match)
    for match in _RAP_ID_PATTERN.finditer(text):
        push("rap_id", match)
    for match in _OS_SYMBOL_PATTERN.finditer(text):
        push("os_symbol", match)
    for match in _DOI_PATTERN.finditer(text):
        push("doi", match)
    for match in _PMID_PATTERN.finditer(text):
        push("pmid", match, group=1)

    seen: set[str] = set()
    unique: list[LexiconMatch] = []
    for item in sorted(matches, key=lambda m: (m.start, -(m.end - m.start))):
        if item.value in seen:
            continue
        seen.add(item.value)
        unique.append(item)
    return unique


def _dictionary_matches(text: str) -> list[LexiconMatch]:
    """词典命中：拉丁词词边界 + 大小写不敏感；中文直接子串。长词优先、区间去重叠。"""
    raw: list[tuple[int, int, str, str]] = []
    latin, cjk = _compiled_dictionary()
    for pattern, label in latin:
        for match in pattern.finditer(text):
            raw.append((match.start(), match.end(), match.group(0), label))
    for term, label in cjk:
        start = 0
        while True:
            index = text.find(term, start)
            if index < 0:
                break
            raw.append((index, index + len(term), term, label))
            start = index + len(term)

    # 长词优先占位，重叠的短词丢弃（"quantitative real-time PCR" 优先于 "RT-PCR" 不同址时不冲突）
    accepted: list[tuple[int, int, str, str]] = []
    for start, end, surface, label in sorted(raw, key=lambda item: (item[0], -(item[1] - item[0]))):
        if any(not (end <= s or start >= e) for s, e, _, _ in accepted):
            continue
        accepted.append((start, end, surface, label))
    return [
        LexiconMatch(surface=surface, label=label, start=start, end=end, kind="dictionary", value=surface)
        for start, end, surface, label in sorted(accepted)
    ]


def pre_annotate(text: str) -> list[LexiconMatch]:
    """确定性预标注：标识符 + 词典 + 数值发育期，按出现位置排序。"""
    matches: list[LexiconMatch] = []
    matches.extend(extract_identifiers(text))
    matches.extend(_dictionary_matches(text))
    for match in _DAP_PATTERN.finditer(text):
        matches.append(
            LexiconMatch(
                surface=match.group(0),
                label="DevelopmentStage",
                start=match.start(),
                end=match.end(),
                kind="dictionary",
                value=match.group(0),
            )
        )
    for match in _DAP_LONG_PATTERN.finditer(text):
        matches.append(
            LexiconMatch(
                surface=match.group(0),
                label="DevelopmentStage",
                start=match.start(),
                end=match.end(),
                kind="dictionary",
                value=match.group(0),
            )
        )
    return sorted(matches, key=lambda m: (m.start, m.end))


def identifier_values(matches: list[LexiconMatch]) -> set[str]:
    """G3 门禁的比对集合：全部标识符字面值。"""
    return {match.value for match in matches if match.kind == "identifier" and match.value}


_IDENTIFIER_FULLMATCHES: tuple[tuple[str, re.Pattern], ...] = (
    ("msu_id", re.compile(r"LOC_Os\d{2}g\d{5,7}")),
    ("rap_id", re.compile(r"Os\d{2}g\d{7}")),
    ("os_symbol", _OS_SYMBOL_PATTERN),
    ("doi", re.compile(r"10\.\d{4,9}/[^\s]+", re.IGNORECASE)),
)


def identifier_kind(surface: str) -> str | None:
    """判定 surface 是否标识符形态（G3 前置）：返回标识符种类或 None。

    PMID 的裸数字形态不在此判定（只有带 ``PMID`` 前缀的上下文才可信），
    避免 G3 把普通数值误伤。
    """
    stripped = surface.strip()
    for kind, pattern in _IDENTIFIER_FULLMATCHES:
        if pattern.fullmatch(stripped):
            return kind
    return None


def lexicon_snapshot() -> dict[str, Any]:
    """词典规模快照（抽取 metadata 记录用，保证可复现性审计）。"""
    return {
        "lexicon_version": LEXICON_VERSION,
        "identifier_kinds": sorted(IDENTIFIER_LABELS),
        "dictionary_labels": {
            label: sum(len(terms) for terms in groups.values()) for label, groups in DICTIONARY_LEXICONS.items()
        },
    }


def lexicon_content_digest() -> str:
    """词典内容 digest（R2a）：对全部词条内容做内容寻址哈希。

    进抽取指纹（替代裸 LEXICON_VERSION 字符串）：``merge_lexicon_terms`` 运行时扩词
    立即改变 digest → 缓存自动失效；merge 进来又移除同内容则 digest 不变（语义未变，
    不触发无谓重抽）。词条顺序不敏感，增删敏感。
    """
    import hashlib

    lines = [f"{LEXICON_VERSION}"]
    for label in sorted(DICTIONARY_LEXICONS):
        for key in sorted(DICTIONARY_LEXICONS[label]):
            terms = "|".join(sorted(DICTIONARY_LEXICONS[label][key]))
            lines.append(f"{label}:{key}:{terms}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:32]
