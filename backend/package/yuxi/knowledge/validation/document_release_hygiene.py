"""Narrow deterministic publication checks, not a semantic entailment judge."""

from __future__ import annotations

import re
from typing import Any

_UNFINISHED = re.compile(r"[（(][^()（）\n]{0,160}[？?][^()（）\n]{0,160}[）)]|(?:或为|或者为)\s*[-+]?\d")
_CONFLICT = re.compile(
    r"(?:不同|各).{0,24}(?:图注|题注|正文|来源|证据).{0,35}(?:差异|矛盾|不一致)|"
    r"(?:图注|题注|正文|来源|证据).{0,24}(?:表述|结论).{0,16}(?:存在差异|相互矛盾|不一致)"
)
_REF = re.compile(r"\[E\d{1,3}\]|〔证据(E\d{1,3})[^〕]*〕")
# 后端签发的图表锚点芯片（ADR-0008）：只从可信题注注册表+血统键签发，其存在
# 即证明该行的图表绑定——不得把图表芯片绑定的因果句误判为"无绑定断言"。
_FIGURE_CHIP = re.compile(r"〔图表F\d+[^〕]*〕")
_FIGURE_TABLE_QUESTION = re.compile(r"Figure|Table|Fig\.|图注|表格|补充图|哪个图|哪几张图|哪些图|图\s*S?\d", re.I)
_ASSERTION = re.compile(
    r"导致|造成|源于|归因|因而|因此|证明|排除|支持|表明|显示|显著|机制|互作|协同|"
    r"caus(?:e|es|ed)|because|demonstrat|significant|exclud|rule[sd]?\s+out",
    re.I,
)
_EXCLUSION = re.compile(
    r"(?:而非|并非|不是|排除).{0,12}脱靶|非脱靶|(?:exclud|rule[sd]?\s+out).{0,24}off[- ]target", re.I
)
_OFF_TARGET_SOURCE = re.compile(r"off[- ]target|脱靶", re.I)
_LIMITATION = re.compile(r"^\s*(?:>\s*)?(?:证据不足|无法|目前不能|本轮未获得|尚不能|未获得直接依据|已省略)")
_OMISSION_NOTICE = "已省略未定稿或缺少绑定来源支持的推断"
_OMISSION_NOTICE_FULL = f"> {_OMISSION_NOTICE}；以下仅保留可引用内容，未获直接依据的具体机制不能确定。"


def _quote_index(citations: list[dict[str, Any]]) -> tuple[dict[str, str], list[str]]:
    by_ref = {str(row.get("ref")): str(row.get("_quote") or row.get("quote_head") or "") for row in citations}
    frozen = [quote for row in citations if row.get("locatable") and len(quote := str(row.get("_quote") or "")) >= 20]
    return by_ref, frozen


def guard_document_release_hygiene(
    text: str, citations: list[dict[str, Any]], *, question: str = ""
) -> tuple[str, dict[str, Any]]:
    """Remove unfinished alternatives and unsupported cross-source conflicts.

    Callers must protect verified verbatim quotations first. Two bound source
    excerpts are necessary, not sufficient, for a cross-source contradiction.
    This check must never be reported as full scientific entailment validation.
    """
    by_ref, _ = _quote_index(citations)
    output = []
    reasons: dict[str, int] = {}
    require_bound_claims = bool(_FIGURE_TABLE_QUESTION.search(question))
    for line in str(text or "").splitlines():
        reason = None
        refs = {match.group(1) or match.group(0)[1:-1] for match in _REF.finditer(line)}
        quotes = {by_ref[ref] for ref in refs if ref in by_ref and by_ref[ref]}
        if _UNFINISHED.search(line):
            reason = "UNFINISHED_ALTERNATIVE"
        elif require_bound_claims and _EXCLUSION.search(line) and not any(_OFF_TARGET_SOURCE.search(q) for q in quotes):
            reason = "EXCLUSION_REQUIRES_EXPLICIT_SOURCE"
        elif (
            require_bound_claims
            and line.strip()
            and not line.lstrip().startswith(("#", "YUXIVERBATIM"))
            and not _LIMITATION.search(line)
            and _ASSERTION.search(line)
            and not quotes
            and not _FIGURE_CHIP.search(line)
        ):
            reason = "ASSERTION_REQUIRES_BOUND_SOURCE"
        elif _CONFLICT.search(line):
            if len(quotes) < 2:
                reason = "CONFLICT_REQUIRES_TWO_SOURCES"
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
        else:
            output.append(line)
    # Do not leave empty answer sections after removing a whole assertion.
    retained = []
    for index, line in enumerate(output):
        if line.lstrip().startswith("#"):
            next_content = next((item.strip() for item in output[index + 1 :] if item.strip()), "")
            if not next_content or next_content.startswith("#"):
                continue
        retained.append(line)
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(retained)).strip()
    if reasons:
        cleaned += "\n\n" + _OMISSION_NOTICE_FULL
    return cleaned, {
        "version": "document-release-hygiene.v1",
        "status": "DEGRADED" if reasons else "PASS",
        "removed_line_count": sum(reasons.values()),
        "reason_counts": reasons,
        "bound_claims_required": require_bound_claims,
    }


def strip_unbound_assertions_after_render(
    text: str, citations: list[dict[str, Any]], *, question: str = ""
) -> tuple[str, dict[str, Any]]:
    """终态无绑定断言清理（发布顺序收口）。

    在引用渲染管线末端**与**逐 Claim 授权执行（``enforce_explanation_grounding``
    会拆除接地后缀/删句）之后调用：渲染时已绑定的因果断言，可能在后续校验中
    失去行内权威芯片，以"无绑定"形态残留发布。本函数只重放**依赖引用绑定**的
    三类检查（断言须绑定/排除须明示来源/跨源矛盾须双源），不做与引用无关的
    未定稿复查（上游已两查）。

    不可清理的管线自有内容：逐字原文（渲染管线内为 ``YUXIVERBATIM`` token 行，
    chat 层为含冻结引文子串的行——引文本身就是绑定）、标题、blockquote、
    ``（注：`` 提示行、``_LIMITATION`` 边界声明行。幂等：省略说明已存在时不
    重复追加。这是确定性发布检查，不得报告为语义蕴含验证。
    """
    by_ref, frozen_quotes = _quote_index(citations)
    require_bound_claims = bool(_FIGURE_TABLE_QUESTION.search(question))
    output: list[str] = []
    reasons: dict[str, int] = {}
    for line in str(text or "").splitlines():
        stripped_line = line.lstrip()
        if (
            not stripped_line
            or stripped_line.startswith(("#", ">", "（注：", "YUXIVERBATIM"))
            or _LIMITATION.search(line)
            or any(quote in line for quote in frozen_quotes)
        ):
            output.append(line)
            continue
        refs = {match.group(1) or match.group(0)[1:-1] for match in _REF.finditer(line)}
        quotes = {by_ref[ref] for ref in refs if ref in by_ref and by_ref[ref]}
        reason = None
        if require_bound_claims and _EXCLUSION.search(line) and not any(_OFF_TARGET_SOURCE.search(q) for q in quotes):
            reason = "EXCLUSION_REQUIRES_EXPLICIT_SOURCE"
        elif require_bound_claims and _ASSERTION.search(line) and not quotes and not _FIGURE_CHIP.search(line):
            reason = "ASSERTION_REQUIRES_BOUND_SOURCE"
        elif _CONFLICT.search(line) and len(quotes) < 2:
            reason = "CONFLICT_REQUIRES_TWO_SOURCES"
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
        else:
            output.append(line)
    retained = []
    for index, line in enumerate(output):
        if line.lstrip().startswith("#"):
            next_content = next((item.strip() for item in output[index + 1 :] if item.strip()), "")
            if not next_content or next_content.startswith("#"):
                continue
        retained.append(line)
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(retained)).strip()
    disclosed = _OMISSION_NOTICE in cleaned or any(stripped.startswith("（注：") for stripped in cleaned.splitlines())
    if reasons and not disclosed:
        cleaned += "\n\n" + _OMISSION_NOTICE_FULL
    return cleaned, {
        "version": "document-release-hygiene.v2-final",
        "status": "DEGRADED" if reasons else "PASS",
        "removed_line_count": sum(reasons.values()),
        "reason_counts": reasons,
        "bound_claims_required": require_bound_claims,
    }
