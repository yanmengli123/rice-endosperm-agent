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

from yuxi import config
from yuxi.literature.boundaries import assert_not_evidence_capable
from yuxi.literature.notes import prompts, renderers
from yuxi.models.chat import select_model

logger = logging.getLogger(__name__)

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
    model = select_model(model_spec=config.default_model)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    response = await model.call(messages, stream=False)
    return str(response.content or "")


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
    raw = await _complete(system, user)
    data = prompts.parse_compile_json(raw)
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
        raw = await _complete(system, user)
        data = prompts.parse_compile_json(raw)
    return renderers.render_relations(meta, data, related_ctxs)


def render_l1_note(meta: dict, l1_data: dict) -> str:
    return renderers.render_note(meta, l1_data)


def render_l2_wiki(meta: dict, l2_data: dict) -> str:
    return renderers.render_wiki(meta, l2_data)
