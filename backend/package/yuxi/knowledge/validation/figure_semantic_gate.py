"""图表语义发布门禁（Figure Semantic Gate）——在 F# 签发前拦截不受支持的 Claim。

架构定位（ADR-0008 P3 → 企业级升级）：
    citation_channel 负责安全签发与渲染，不承担科研语义判断。
    本模块在 F# 签发**之前**运行，对已绑定图表锚点的自然语言断言做
    确定性条件匹配 + 实验语境校验。语义裁判（LLM）作为第二级，
    仅接收单条 Claim + 对应题注 + 正文段落，输出封闭枚举。

两级校验：
    第一级（确定性，零 LLM）：
        - 图表标签是否存在于当前冻结 revision
        - 数值是否能映射到表格单元格（数值→行列坐标查找）
        - 比较双方是否来自同一实验条件（control vs heat）
        - "热胁迫变化"是否同时存在两组数据
        - 实验材料是否匹配（cr-myb73 vs osmyb73 命名一致性）
    第二级（受约束语义裁判，预留接口）：
        - 单条 Claim + 题注 + 正文段 → SUPPORTED/PARTIAL/UNSUPPORTED/CONFLICT

失败关闭矩阵（"删除错误"而非"标注错误"）：
    找不到图号       → 不签 F#，不描述图内容
    有题注、无资产    → 可引用题注，但明确"仅完成题注联动，未取得图像"
    语义不匹配       → 删除该断言句，不签 F#
    机制证据不足     → 改措辞（"作者提出"），不删除
    表格缺某条件    → 拒绝比较，不用基线数据代替变化数据
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ClaimType(StrEnum):
    DIRECT_OBSERVATION = "DIRECT_OBSERVATION"
    NUMERIC = "NUMERIC"
    COMPARISON = "COMPARISON"
    MECHANISM = "MECHANISM"
    HYPOTHESIS = "HYPOTHESIS"


class Verdict(StrEnum):
    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    UNSUPPORTED = "UNSUPPORTED"
    CONFLICT = "CONFLICT"


class VisualStatus(StrEnum):
    """锚点视觉状态（载荷 ``figure_refs[].visual_status`` 的闭集）。

    必须与 ``chat_service._visual_status_for_ref`` 的**实际产出**逐一对应——
    2026-09-27 修复：此前载荷会发出 ``VERIFIED_WITH_TABLE``，而枚举未定义该成员
    （跨端契约违例：任何按枚举校验的客户端会把它判为非法值），且
    ``REFERENCE_ONLY`` 永不产出、``_visual_status_for_ref`` 被字典重复键覆盖成
    死代码。现在四态齐备且有唯一计算点。
    """

    VERIFIED_WITH_ASSET = "VERIFIED_WITH_ASSET"  # 题注已验证且取得图片资产
    VERIFIED_WITH_TABLE = "VERIFIED_WITH_TABLE"  # 题注已验证且取得表格卡片
    VERIFIED_CAPTION_ONLY = "VERIFIED_CAPTION_ONLY"  # 题注已验证，无图/表卡片
    REFERENCE_ONLY = "REFERENCE_ONLY"  # 仅引用，无题注证据（防御分支）


@dataclass
class FigureClaim:
    """一条与图表关联的自然语言断言（从回答正文中提取）。"""

    text: str
    claim_type: ClaimType
    figure_labels: list[str] = field(default_factory=list)
    numeric_values: list[str] = field(default_factory=list)
    condition_keywords: list[str] = field(default_factory=list)
    sentence_start: int = 0
    sentence_end: int = 0


@dataclass
class SemanticVerdict:
    """门禁裁决结果。"""

    verdict: Verdict
    reason_code: str
    claim: FigureClaim | None = None
    visual_status: VisualStatus = VisualStatus.REFERENCE_ONLY
    required_language: str = "FACT"  # FACT | INFERENCE | HYPOTHESIS
    missing_dimensions: list[str] = field(default_factory=list)


# ---- 实验条件词表（确定性匹配，零 LLM） ----

_CONDITION_PATTERNS = {
    "heat": re.compile(r"heat\s*stress|热胁迫|高温|heat\b", re.IGNORECASE),
    "control": re.compile(r"control|对照|常温|normal\b|wild.?type\b|WT\b", re.IGNORECASE),
    "cold": re.compile(r"cold\s*stress|低温|冷胁迫", re.IGNORECASE),
    "drought": re.compile(r"drought|干旱", re.IGNORECASE),
    "salt": re.compile(r"salt|盐胁迫|NaCl", re.IGNORECASE),
}

_MECHANISM_KEYWORDS = re.compile(
    r"regulat| pathway| signal| mediates|调控|信号|通路|介导|mechanism|机制|"
    r"negative regulator|positive regulator|负调控|正调控|promoter|启动子|结合|bind",
    re.IGNORECASE,
)

_NUMERIC_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*(?:%|℃|°C|mm|cm|mg|g|倍|个百分点|fold)", re.IGNORECASE)

_COMPARISON_KEYWORDS = re.compile(
    r"higher|lower|increase|decrease|更多|更少|升高|降低|增加|减少|大于|小于|"
    r"显著|significant|compared|比较|versus|vs\.?|相对",
    re.IGNORECASE,
)


def classify_claim(text: str) -> ClaimType:
    """确定性分类：一条断言属于哪种 Claim 类型。

    优先级（从高到低）：HYPOTHESIS > MECHANISM > COMPARISON > NUMERIC > DIRECT。
    假说标记词必须最先检查——"作者提出调控模型"是假说不是机制断言。
    """
    # 假说标记最强：有明确推测措辞的永远是 HYPOTHESIS
    if re.search(r"可能|提示|suggest|propose|hypothes|推测|假说|作者认为|作者提出", text, re.IGNORECASE):
        return ClaimType.HYPOTHESIS
    if _MECHANISM_KEYWORDS.search(text):
        return ClaimType.MECHANISM
    # COMPARISON 需要显式比较结构（双方+方向词），不只是"增加"
    if _NUMERIC_PATTERN.search(text) and re.search(
        r"compared\s+to|versus|vs\.?|比|较|相对|高于|低于|大于|小于|more\s+than|less\s+than", text, re.IGNORECASE
    ):
        return ClaimType.COMPARISON
    if _NUMERIC_PATTERN.search(text):
        return ClaimType.NUMERIC
    return ClaimType.DIRECT_OBSERVATION


def extract_conditions(text: str) -> set[str]:
    """提取断言中涉及的实验条件关键词。"""
    return {name for name, pattern in _CONDITION_PATTERNS.items() if pattern.search(text)}


def validate_condition_coverage(
    claim: FigureClaim,
    available_conditions: set[str],
) -> SemanticVerdict | None:
    """确定性校验：断言需要的条件是否都在可用数据中。

    场景（Q4 实测）：用户问"热胁迫下垩白度变化"，但 Table 1 只有常温数据 →
    需要 heat 条件但 available 里没有 → 拒绝该 Claim。

    **空输入 fail-open（2026-09-27 修复）**：``available_conditions`` 为空表示
    "调用方没有提供条件数据"，不是"任何条件都缺失"。历史实现把空集当"全部缺失"
    → ``missing = needed - ∅ = needed`` → 任何含条件词（热胁迫/常温/WT…）的句子
    被判 CONDITION_MISMATCH，而生产调用点恰好**不传**该参数
    （citation_channel 步骤 1b 在 chat 层表格投影**之前**运行，拿不到表格数据）
    → 真实回答被删句（实测量产事故：205 字符段 → 只剩 20 字符标题；msg 4024
    留下悬空空列表项）。无数据 ≠ 断言不成立：不做判断是唯一诚实的裁决。
    """
    if available_conditions is None:
        return None  # 未传条件数据（调用方无表格）——不做判断是唯一诚实的裁决
    # 空集 set() ≠ None：表格在但无条件列（如 Table 1 单条件基因型比较）
    # → 应正常判定：文本含热胁迫但表格无热胁迫列 = CONDITION_MISMATCH
    needed = extract_conditions(claim.text)
    missing = needed - available_conditions
    if missing:  # 所有类型都检查：直接观察也无法在缺条件的数据上断言
        return SemanticVerdict(
            verdict=Verdict.UNSUPPORTED,
            reason_code="CONDITION_MISMATCH",
            claim=claim,
            missing_dimensions=sorted(missing),
        )
    return None


def validate_numeric_cell_mapping(
    claim: FigureClaim,
    table_rows: list[list[dict[str, Any]]],
) -> SemanticVerdict | None:
    """确定性校验：数值是否能映射到表格单元格。

    提取 Claim 中的精确数值，检查是否在表格行列中找到匹配。
    找不到 → 数值不可溯源 → 拒绝（防编造数字）。

    **空输入 fail-open（2026-09-27 修复）**：``table_rows`` 为空表示"本轮没有已
    发布表格"，不是"所有数值都不在表里"。历史实现下 ``table_numbers = ∅`` →
    ``unmapped = claim_numbers`` → 判据 ``len(unmapped) > len(claim_numbers)//2``
    恒真 → 任何数值句被判 NUMERIC_NOT_IN_SOURCE 并删除（实测：「正常条件下垩白率
    为 12.1%。」→ 输出空串）。数值溯源只有在**拿到表格**时才有意义。
    """
    if not table_rows:
        return None
    if claim.claim_type not in (ClaimType.NUMERIC, ClaimType.COMPARISON):
        return None
    # 排除基因符号后缀数字（OsMYB73 的 "73" 不是测量值）
    gene_numbers = set(re.findall(r"[A-Za-z]{2,}(\d+)", claim.text))
    claim_numbers = {n for n in re.findall(r"\d+(?:\.\d+)?", claim.text) if n not in gene_numbers}
    table_numbers: set[str] = set()
    for row in table_rows:
        for cell in row:
            for num in re.findall(r"\d+(?:\.\d+)?", str(cell.get("text") or "")):
                table_numbers.add(num)
    # Claim 中的每个精确数值都应能在表格中找到（或为可推导的差值——后者由语义层处理）
    unmapped = claim_numbers - table_numbers
    if unmapped and len(unmapped) > len(claim_numbers) // 2:
        return SemanticVerdict(
            verdict=Verdict.UNSUPPORTED,
            reason_code="NUMERIC_NOT_IN_SOURCE",
            claim=claim,
            missing_dimensions=[f"unmapped:{n}" for n in sorted(unmapped)],
        )
    return None


def determine_visual_status(
    *,
    caption_verified: bool,
    has_asset: bool,
    semantic_passed: bool,
    has_table: bool = False,
) -> VisualStatus:
    """四态视觉状态判定（图像与结构化表卡分立）。"""
    if caption_verified and has_table and semantic_passed:
        return VisualStatus.VERIFIED_WITH_TABLE
    if caption_verified and has_asset and semantic_passed:
        return VisualStatus.VERIFIED_WITH_ASSET
    if caption_verified and semantic_passed:
        return VisualStatus.VERIFIED_CAPTION_ONLY
    return VisualStatus.REFERENCE_ONLY


def visual_status_label(status: VisualStatus) -> str:
    """用户可读的状态文案。"""
    if status == VisualStatus.VERIFIED_WITH_ASSET:
        return "已验证图文引用"
    if status == VisualStatus.VERIFIED_WITH_TABLE:
        return "已验证表格引用"
    if status == VisualStatus.VERIFIED_CAPTION_ONLY:
        return "已定位题注（未取得图像）"
    return "图表引用"


def run_semantic_gate(
    text: str,
    *,
    figure_labels_in_registry: set[str] | None = None,
    available_conditions: set[str] | None = None,
    table_rows: list[list[dict[str, Any]]] | None = None,
    caption_assets_available: bool = False,
) -> list[SemanticVerdict]:
    """对回答正文运行图表语义门禁（第一级，全确定性）。

    返回每条图表相关断言的裁决列表。调用方（chat_service 或
    citation_channel 的签发前挂接点）据此决定：签发/降级/删除/改措辞。

    参数：
        figure_labels_in_registry: 本轮注册表中可用的图表编号集合
        available_conditions: 当前证据覆盖的实验条件（control/heat/…）
        table_rows: 已发布表格的行列 JSON（P2 投影产物）
        caption_assets_available: 该图表是否有可发布图片资产
    """
    verdicts: list[SemanticVerdict] = []
    labels = figure_labels_in_registry or set()
    conditions = available_conditions  # preserve None vs set() — validate_condition_coverage 区分
    rows = table_rows or []

    # 按句扫描，找含图表编号或数值的断言——小数点不是句界（与 citation_channel
    # 的 _DECIMAL_DOT_PATTERN 同一问题），先哨兵保护再切分，处理完还原
    _decimal_sentinel = "\x00SG_DECIMAL\x00"
    protected = re.sub(r"(?<=\d)[ \t]*\.[ \t]*(?=\d)", _decimal_sentinel, str(text or ""))
    for match in re.finditer(r"[^.!?。！？\n]+[.!?。！？]?", protected):
        sentence = match.group(0).replace(_decimal_sentinel, ".").strip()
        if not sentence:
            continue
        # 是否含图表编号
        fig_refs = re.findall(r"(?:Fig(?:ure)?\.?|Table|图|表)\s*S?\d+", sentence, re.IGNORECASE)
        # 是否含精确数值
        has_numeric = bool(_NUMERIC_PATTERN.search(sentence))
        if not fig_refs and not has_numeric:
            continue

        claim_type = classify_claim(sentence)
        claim = FigureClaim(
            text=sentence,
            claim_type=claim_type,
            figure_labels=fig_refs,
            numeric_values=re.findall(r"\d+(?:\.\d+)?", sentence),
            sentence_start=match.start(),
            sentence_end=match.end(),
        )

        # 校验 1：图表标签是否在注册表
        unknown_labels = [
            label for label in fig_refs if label.lower().strip(".") not in {known.lower() for known in labels}
        ]
        if unknown_labels and claim_type in (ClaimType.DIRECT_OBSERVATION, ClaimType.NUMERIC):
            verdicts.append(
                SemanticVerdict(
                    verdict=Verdict.UNSUPPORTED,
                    reason_code="UNKNOWN_FIGURE_LABEL",
                    claim=claim,
                )
            )
            continue

        # 校验 2：条件覆盖
        condition_verdict = validate_condition_coverage(claim, conditions)
        if condition_verdict:
            verdicts.append(condition_verdict)
            continue

        # 校验 3：数值溯源
        numeric_verdict = validate_numeric_cell_mapping(claim, rows)
        if numeric_verdict:
            verdicts.append(numeric_verdict)
            continue

        # 全部通过 → 确定视觉状态
        status = determine_visual_status(
            caption_verified=bool(fig_refs),
            has_asset=caption_assets_available,
            semantic_passed=True,
        )
        verdicts.append(
            SemanticVerdict(
                verdict=Verdict.SUPPORTED,
                reason_code="ok",
                claim=claim,
                visual_status=status,
                required_language="FACT" if claim_type != ClaimType.MECHANISM else "INFERENCE",
            )
        )

    return verdicts


__all__ = [
    "ClaimType",
    "FigureClaim",
    "SemanticVerdict",
    "Verdict",
    "VisualStatus",
    "classify_claim",
    "determine_visual_status",
    "extract_conditions",
    "run_semantic_gate",
    "validate_condition_coverage",
    "validate_numeric_cell_mapping",
    "visual_status_label",
]
