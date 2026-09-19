"""括号分诊（B1）：科研文献括号补充说明的确定性分类——「先分类，再归一」。

括号内容不是同义词的大杂烩，四类下游路由完全不同：

- ``ALIAS_TAXON``         学名/品种名（真同义归一候选，进文档词典）；
- ``ABBREV_DEFINITION``   缩写定义点 "high-temperature treatment (HT)"（进缩写词典）；
- ``EQUIPMENT_ATTRIBUTE`` 设备型号与厂家（Method 实体属性，绝不上图谱边）；
- ``CONDITION_PARAMETER`` 数值+单位（N 元组 condition 维度素材）；
- ``LOCAL_RULE``          局部时间换算/实验规则（文档 overlay，永不进全局词典）；
- ``UNCERTAIN``           规则无法裁决（保留统计，不注入任何通道）。

纯规则、零模型、零 I/O；LLM 兜底分类是后续演进位（source=LLM 的条目走人工审核）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BRACKET_TRIAGE_VERSION = "bracket_triage_v1"

KIND_ALIAS_TAXON = "ALIAS_TAXON"
KIND_ABBREV_DEFINITION = "ABBREV_DEFINITION"
KIND_EQUIPMENT_ATTRIBUTE = "EQUIPMENT_ATTRIBUTE"
KIND_CONDITION_PARAMETER = "CONDITION_PARAMETER"
KIND_LOCAL_RULE = "LOCAL_RULE"
KIND_UNCERTAIN = "UNCERTAIN"

# 单层括号（中英），内容不含换行与嵌套括号，长度 1..160
_BRACKET_PATTERN = re.compile(r"([（(])([^（）()\n]{1,160})([）)])")

# 学名/品种：属名缩写 O. sativa / 全属 Oryza sativa + L./cv./var. 或含 cv.
_TAXON_PATTERN = re.compile(
    r"^(?:[A-Z][a-z]{2,}|[A-Z]\.?)\s+[a-z]{2,}[a-z\s.]*"
    r"(?:\s+[Ll]\.|cv\.|var\.|subsp\.|ssp\.)"
)
_TAXON_CV_HINT = re.compile(r"\bcv\.?\s+\S+")

# 设备：厂家词表（小写比对）或 型号形态（字母+数字混合且含数字串）
_EQUIPMENT_VENDORS = (
    "leica",
    "zeiss",
    "olympus",
    "nikon",
    "thermo",
    "fisher",
    "beckman",
    "eppendorf",
    "shimadzu",
    "hitachi",
    "agilent",
    "biorad",
    "bio-rad",
    "illumina",
    "sartorius",
    "millipore",
    "whatman",
    "perkinelmer",
    "syngene",
    "tanon",
)
_EQUIPMENT_MODEL = re.compile(r"\b[A-Za-z]{1,10}-?\d{1,6}[A-Za-z]{0,6}\b")

# 数值+单位（条件参数）：允许前导数值（含小数/负号）+ 单位词
_CONDITION_PARAMETER = re.compile(
    r"^\s*[-+]?\d+(?:\.\d+)?(?:\s*[-–~至]\s*[-+]?\d+(?:\.\d+)?)?\s*"
    r"(?:°C|%|mM|mmol|μmol|umol|µmol|mg|g|kg|kDa|Da|rpm|kHz|Hz|V|mV|mA|W|"
    r"L|mL|μL|ml|μl|ul|min|h|d|wk|week|w/v|v/v|ppm|bar|kPa|MPa)\b"
)
_PURE_NUMBER = re.compile(r"^\s*[-+]?\d+(?:\.\d+)?\s*$")

# 局部规则：显式定义/换算记号
_LOCAL_RULE_HINT = re.compile(r"[=＝]|即|指|定义为|相当于")

# 缩写形态：1-12 个字符、含大写字母、无空格或单个点（HT / DAH / OsNF-YB1 / qRT-PCR）
_ABBREV_SHAPE = re.compile(r"^[A-Za-z][A-Za-z0-9;&-]{0,11}$")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]")
# 品种名/先行词的首尾连接字：以秋田小町（…）→ 秋田小町
_LEADING_CONNECTORS = "以用经将在于按对从与和及"
_TRAILING_CONNECTORS = "的之下中后前内为"


@dataclass(frozen=True)
class BracketAnnotation:
    """一次括号分诊。expansion 仅 ABBREV_DEFINITION 携带（匹配到的前置展开式）。"""

    kind: str
    surface: str  # 含括号的原文片段
    inner: str
    start: int
    end: int
    expansion: str | None = None


def classify_brackets(text: str) -> list[BracketAnnotation]:
    """对全文所有单层括号做确定性分诊，按出现位置排序。"""
    annotations: list[BracketAnnotation] = []
    for match in _BRACKET_PATTERN.finditer(text):
        inner = match.group(2).strip()
        if not inner:
            continue
        start, end = match.start(), match.end()
        before = text[max(0, start - 80) : start]
        kind, expansion = _classify(inner, before)
        annotations.append(
            BracketAnnotation(
                kind=kind,
                surface=match.group(0),
                inner=inner,
                start=start,
                end=end,
                expansion=expansion,
            )
        )
    return annotations


def _classify(inner: str, before: str) -> tuple[str, str | None]:
    # 1. 学名/品种名（优先级最高：含 cv. 的几乎不可能是别的）
    if _TAXON_PATTERN.search(inner) or _TAXON_CV_HINT.search(inner):
        return KIND_ALIAS_TAXON, None
    # 2. 缩写定义点：缩写形态 + 前文能找到展开式
    if _ABBREV_SHAPE.match(inner) and re.search(r"[A-Z]", inner):
        expansion = _match_expansion(inner, before)
        if expansion:
            return KIND_ABBREV_DEFINITION, expansion
    # 3. 局部规则：显式定义/换算记号
    if _LOCAL_RULE_HINT.search(inner):
        return KIND_LOCAL_RULE, None
    # 4. 设备属性：厂家或型号
    lowered = inner.lower()
    if any(vendor in lowered for vendor in _EQUIPMENT_VENDORS) or (
        _EQUIPMENT_MODEL.search(inner) and re.search(r"\d", inner) and not _CONDITION_PARAMETER.match(inner)
    ):
        return KIND_EQUIPMENT_ATTRIBUTE, None
    # 5. 条件参数：数值(+区间)+单位 或纯数值
    if _CONDITION_PARAMETER.match(inner) or _PURE_NUMBER.match(inner):
        return KIND_CONDITION_PARAMETER, None
    return KIND_UNCERTAIN, None


def _match_expansion(abbrev: str, before: str) -> str | None:
    """在括号前的文本里找缩写展开式：字母按序是前文词首字母（英文惯例），
    或紧邻括号的是中文短语（中文文献「高温处理（HT）」惯例）。"""
    letters = [char.lower() for char in abbrev if char.isalpha()]
    if not letters:
        return None
    if _CJK_RUN.search(before[-24:]):
        cjk = re.findall(r"[\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9-]{1,23}", before[-24:])
        if cjk:
            # 「幼苗经高温处理（HT）」→ 取连接字（经/用/以/在/按/于/将）后的末段短语，
            # 再剥尾部方位字（下/中/时/后…）
            segments = [segment for segment in re.split(r"[经用以在按于将]", cjk[-1]) if segment.strip()]
            phrase = segments[-1] if segments else cjk[-1]
            return re.sub(r"[下中时后期间里]$", "", phrase) or phrase

    words = re.findall(r"[A-Za-z][A-Za-z-]*", before)
    if not words:
        return None
    # 从后往前取 len(letters) 个词，检查首字母按序匹配
    span = words[-len(letters) :] if len(words) >= len(letters) else words
    if len(span) != len(letters):
        return None
    for word, letter in zip(span, letters, strict=True):
        if not word or word[0].lower() != letter:
            return None
    phrase_start = before.lower().rfind(span[0].lower(), 0, before.lower().rfind(span[-1].lower()) + len(span[-1]))
    if phrase_start < 0:
        return None
    return before[phrase_start:].strip(" ,;:-–")


def taxon_alias_target(text: str, annotation: BracketAnnotation) -> str | None:
    """ALIAS_TAXON 的配对品种名：紧邻括号前的词（中文品种名或拉丁栽培种名）。

    「秋田小町（O. sativa L. cv. Akitakomachi）」→ 秋田小町；
    「Akitakomachi (O. sativa L. cv. Akitakomachi)」→ Akitakomachi。
    """
    before = text[: annotation.start].rstrip()
    if not before:
        return None
    match = re.search(r"[\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9 .'-]*$", before)
    if not match:
        return None
    target = match.group(0).strip(" .,-:'")
    target = target.lstrip(_LEADING_CONNECTORS).rstrip(_TRAILING_CONNECTORS)
    return target or None
