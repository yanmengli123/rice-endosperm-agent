"""L1/L2/L3 编译器（P2，ADR-0010 决议 3/4/11）。

语义移植自 PaperAgent ``paperkb/compile.py``（提示词/合并调用/JSON 抢救），
LLM 客户端换 Yuxi 模型链（``yuxi.models.select_model``，**不引 openai 直连**）。

不变量 L0-3 执行点（ADR-0010）：
- 输入：Authority Plane 的解析文本（reader render 的 document.json 段落——
  只读引用，派生物不进证据通道）；
- 输出：literature_notes 派生产品（编译产物**永不写 evidence_spans、不进
  Milvus**，由 test_compiler_gate 钉死）；出口经
  ``assert_not_evidence_capable("literature_notes")`` 断言；
- Claim 数保持 0 是**正确行为**（ADR-0001 §8：LLM 自由生成文本不得成为
  VerifiedClaim——防止后人"补齐 Claim"时把编译文本塞进证据通道）。
"""

from __future__ import annotations

import logging
from typing import Any

from yuxi.literature.boundaries import assert_not_evidence_capable
from yuxi.literature.notes import prompts, renderers

logger = logging.getLogger(__name__)

# 注意：select_model 必须在调用时延迟导入——顶层导入会形成
# yuxi.models.chat → yuxi.agents（toolkits/__init__ 拉起本包）→ 本包
# → yuxi.models.chat 的循环（P6b 工具注册引入，test_benchmark_generation 收集期暴露）。

LITERATURE_NOTES_KB_TYPE = "literature_notes"

_L1_KEYS = {
    "one_liner",
    "background",
    "method",
    "result",
    "conclusion",
    "innovation",
    "limitation",
    "concepts",
    "tags",
    "ai_value",
    "topic_score",
}


def split_l1_l2(data: dict) -> tuple[dict, dict | None]:
    """拆分合并编译输出（上游 _split_l1_l2_merged 口径）：wiki 缺失/过短 → L2 为 None。"""
    l1 = {k: v for k, v in data.items() if k in _L1_KEYS}
    wiki = data.get("wiki")
    l2 = None
    if isinstance(wiki, str) and len(wiki.strip()) > 50:
        l2 = {"wiki": wiki, "concepts": data.get("concepts") or []}
    return l1, l2


async def _complete(system: str, user: str) -> str:
    """经 Yuxi 模型链完成一次补全（system+user 两消息，前缀缓存友好）。"""
    from yuxi.literature.llm_gateway import call  # noqa: PLC0415 - 延迟导入防循环（见模块头）

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    response = await call(messages)
    return str(response.content or "")


async def complete_json(system: str, user: str) -> tuple[dict, str]:
    """补全并解析 JSON；解析失败带错误上下文重问恰好一次（模型级修复）。

    真实事故（2026-10-04）：多模型长中文输出畸形（含超出确定性修复级联的串内
    未转义引号），直接烧穿编译/翻译任务的 attempt 预算。确定性修复
    （parse_compile_json）先兜底，仍失败则把原始输出与解析错误回喂模型重问；
    重问的调用同样经治理网关计费。两次都失败才向上抛 ValueError。
    """
    raw = await _complete(system, user)
    try:
        return prompts.parse_compile_json(raw), raw
    except ValueError as exc:
        corrective = (
            f"{user}\n\n---\n你上一次的输出不是合法 JSON（解析错误：{exc}）。\n"
            "请重新输出修正后的完整 JSON：只输出 JSON 本身，不要解释、不要代码围栏，"
            "字符串内部的双引号必须转义（写成反斜杠加双引号），不要在字符串里保留未转义的换行。"
        )
        retry_raw = await _complete(system, corrective)
        return prompts.parse_compile_json(retry_raw), retry_raw


async def compile_l1_l2(
    meta: dict,
    paragraphs: list[str],
    *,
    journal_meta: str = "",
) -> dict[str, Any]:
    """L1+L2 合并编译（上游默认路径）。返回 {l1, l2, ai_value, topic_score, raw}。

    l2 可能为 None（模型未产出有效 wiki 段——上游口径：回落仅 L1）。
    """
    assert_not_evidence_capable(LITERATURE_NOTES_KB_TYPE)  # 出口前提：派生产品身份
    prompt = prompts.prompt_l1_l2_merged(meta, paragraphs, journal_meta)
    system, user = prompts.split_task(prompt)
    data, raw = await complete_json(system, user)
    l1, l2 = split_l1_l2(data)
    if not l1.get("one_liner"):
        raise ValueError("编译输出缺少 one_liner（L1 最低契约）")
    return {
        "l1": l1,
        "l2": l2,
        "ai_value": l1.get("ai_value"),
        "topic_score": l1.get("topic_score"),
        "raw": raw,
    }


async def compile_l3(
    meta: dict,
    self_ctx: str,
    related_ctxs: list[dict],
) -> str:
    """L3 跨文献关系编译 → _relations markdown。

    related_ctxs 形如 [{doi, context, connection}]（context = 对方 L1 摘要）；
    相关文献召回（关键词提取 + 向量/混合检索）由 service 层喂入——P2 最小闭环
    可传空列表（此时产出仅含研究簇概述骨架，语义与上游单文献 L3 一致）。
    """
    assert_not_evidence_capable(LITERATURE_NOTES_KB_TYPE)
    data: dict[str, Any] = {}
    if related_ctxs:
        prompt = prompts.prompt_l3(meta, self_ctx, related_ctxs)
        system, user = prompts.split_task(prompt)
        data, _raw = await complete_json(system, user)
    return renderers.render_relations(meta, data, related_ctxs)


def render_l1_note(meta: dict, l1_data: dict) -> str:
    return renderers.render_note(meta, l1_data)


def render_l2_wiki(meta: dict, l2_data: dict) -> str:
    return renderers.render_wiki(meta, l2_data)
