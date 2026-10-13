"""Versioned BERTopic data contract. Analytical products are never evidence."""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
from collections import Counter, defaultdict

VERSION = "literature-topics-v1"
MAX_DOCUMENTS = 50_000
MAX_TEXT_CHARS = 128 * 1024
MODES = ("papers", "records")
MODELS = ("joint_bge_m3", "chinese_bge_zh", "chinese_bge_m3_comparison")


def prepare_document(key: str, rid: str, row: dict) -> dict:
    def clean(value):
        text = html.unescape(str(value or ""))
        text = re.sub(r"<正>|</?[A-Za-z][^>]*>", " ", text)
        text = re.sub(r"\s+(?:Copyright\s+(?:©|\(C\)|\d{4})|©\s*\d{4}|\(C\)\s*\d{4}).*$", "", text, flags=re.I)
        return re.sub(r"\s+", " ", text).strip()

    title, abstract = clean(row.get("title")), clean(row.get("abstract"))
    text = title + ((" " if title.endswith((".", "!", "?", "。", "！", "？")) else ". ") + abstract if abstract else "")
    if not text or len(text) > MAX_TEXT_CHARS or "\x00" in text:
        raise ValueError("建模文本为空或超过安全预算")
    zh = len(re.findall(r"[\u3400-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return {
        **row,
        "document_key": key,
        "rid": rid,
        "title": title,
        "abstract": abstract,
        "text": text,
        "has_abstract": bool(abstract),
        "language_of_text": "zh" if zh / max(1, zh + latin) > 0.2 else "en",
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def corpus_digest(documents: list[dict]) -> str:
    return hashlib.sha256(
        json.dumps(documents, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def corpus_summary(documents: list[dict]) -> dict:
    return {
        "documents": len(documents),
        "unique_papers": len({row["rid"] for row in documents}),
        "title_only": sum(not row["has_abstract"] for row in documents),
        "languages": dict(Counter(row["language_of_text"] for row in documents)),
        "sources": dict(Counter(row.get("source", "other") for row in documents)),
        "missing_year": sum(row.get("year") is None for row in documents),
    }


def validate_result(result: dict, documents: list[dict]) -> dict:
    """Reject missing/extra/duplicate assignments and fabricated cluster summaries."""
    if result.get("schema_version") != VERSION or not isinstance(result.get("models"), dict):
        raise ValueError("主题结果协议版本不匹配")
    if result.get("product_type") not in (None, "literature_topics"):
        raise ValueError("主题结果不能声明为权威证据产品")
    result["product_type"] = "literature_topics"
    models = result["models"]
    if "joint_bge_m3" not in models or set(models) - set(MODELS):
        raise ValueError("主题模型标识无效")
    for name, model in models.items():
        expected = {d["document_key"] for d in documents if name == "joint_bge_m3" or d["language_of_text"] == "zh"}
        assignments = model.get("assignments", [])
        keys = [row.get("document_key") for row in assignments]
        if len(keys) != len(set(keys)) or set(keys) != expected:
            raise ValueError("逐文献主题结果与授权语料不一致")
        counts = Counter()
        members = {}
        for row in assignments:
            if type(row.get("topic")) is not int or row["topic"] < -1:
                raise ValueError("主题编号无效")
            for field in ("membership", "centroid_cosine"):
                value = row.get(field)
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                    raise ValueError("主题分数必须是有限数值")
            if not 0 <= row["membership"] <= 1 or not -1.00001 <= row["centroid_cosine"] <= 1.00001:
                raise ValueError("主题分数超出定义范围")
            counts[row["topic"]] += 1
            members.setdefault(row["topic"], set()).add(row["document_key"])
            for field in ("x", "y"):
                value = row.get(field)
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError("文献投影坐标必须是有限数值")
        topics = model.get("topics", [])
        if len(topics) > 500:
            raise ValueError("主题数超过 500 个安全预算，请增大最小簇规模")
        if len({t["topic"] for t in topics}) != len(topics):
            raise ValueError("主题汇总编号重复")
        if {t["topic"]: t["count"] for t in topics} != {k: v for k, v in counts.items() if k >= 0}:
            raise ValueError("主题汇总与逐文献归属计数不一致")
        for topic in topics:
            if type(topic.get("topic")) is not int or type(topic.get("count")) is not int or topic["count"] < 1:
                raise ValueError("主题汇总编号与计数必须为整数")
            if not isinstance(topic.get("label"), str) or len(topic["label"]) > 512:
                raise ValueError("主题标签无效")
            for keyword in topic.get("keywords", []):
                if (
                    not isinstance(keyword.get("term"), str)
                    or len(keyword["term"]) > 512
                    or not isinstance(keyword.get("weight"), (int, float))
                    or isinstance(keyword.get("weight"), bool)
                    or not math.isfinite(keyword["weight"])
                    or keyword["weight"] < 0
                ):
                    raise ValueError("主题关键词无效")
            actual_members = members[topic["topic"]]
            if not set(topic.get("representatives", [])) <= actual_members:
                raise ValueError("代表文献不属于对应主题")
        ids = {t["topic"] for t in topics}
        for row in assignments:
            nearest = row.get("nearest_topic")
            if nearest is not None and (type(nearest) is not int or nearest not in ids | {-1}):
                raise ValueError("最近主题编号不属于当前模型")
        _validate_analytics(model, ids, [d for d in documents if d["document_key"] in expected], assignments)
        model["outliers"] = counts[-1]
        model["documents"] = len(assignments)
        language_by_key = {d["document_key"]: d["language_of_text"] for d in documents}
        language_groups = {}
        for row in assignments:
            language_groups.setdefault(language_by_key[row["document_key"]], Counter())[row["topic"]] += 1
        model["language_concentration"] = {
            language: {
                "documents": sum(values.values()),
                "largest_topic": max(values, key=values.get),
                "largest_topic_share": max(values.values()) / sum(values.values()),
            }
            for language, values in language_groups.items()
        }
    return result


def _validate_analytics(model, ids, documents, assignments):
    """Reject misleading chart data before immutable publication, not in the UI."""
    if "similarity" in model:
        pairs, seen = model["similarity"], set()
        if not isinstance(pairs, list) or len(pairs) != len(ids) * (len(ids) - 1) // 2:
            raise ValueError("主题相似度矩阵不完整")
        for pair in pairs:
            if not isinstance(pair, dict) or any(type(pair.get(k)) is not int for k in ("a", "b")):
                raise ValueError("主题相似度索引无效")
            key = tuple(sorted((pair["a"], pair["b"])))
            value = pair.get("cosine")
            if (
                key in seen
                or key[0] == key[1]
                or not set(key) <= ids
                or type(value) not in (int, float)
                or not math.isfinite(value)
                or not -1.00001 <= value <= 1.00001
            ):
                raise ValueError("主题相似度索引或数值无效；离群不是主题质心")
            seen.add(key)
    if "hierarchy" in model:
        rows = model["hierarchy"]
        if not isinstance(rows, list) or len(rows) != max(0, len(ids) - 1):
            raise ValueError("主题层次树不完整")
        sizes = {i: 1 for i in range(len(ids))}
        for index, row in enumerate(rows):
            if (
                not isinstance(row, (list, tuple))
                or len(row) != 4
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in row)
            ):
                raise ValueError("主题层次树结构或数值无效")
            a, b, distance, leaves = row
            if a != int(a) or b != int(b) or a == b or a not in sizes or b not in sizes or not 0 <= distance <= 2:
                raise ValueError("主题层次树子节点无效")
            count = sizes.pop(a) + sizes.pop(b)
            if leaves != count:
                raise ValueError("主题层次树叶节点计数不一致")
            sizes[len(ids) + index] = count
    for topic in model["topics"]:
        frequencies = topic.get("word_frequencies", [])
        seen = set()
        if not isinstance(frequencies, (list, tuple)):
            raise ValueError("主题词频结构无效")
        for pair in frequencies:
            if (
                not isinstance(pair, (list, tuple))
                or len(pair) != 2
                or not isinstance(pair[0], str)
                or not pair[0]
                or len(pair[0]) > 512
                or pair[0] in seen
                or type(pair[1]) is not int
                or pair[1] < 0
            ):
                raise ValueError("主题真实词频必须是唯一词项和非负整数计数")
            seen.add(pair[0])
    if "analysis_year" in model and (
        type(model["analysis_year"]) is not int or not 1 <= model["analysis_year"] <= 9999
    ):
        raise ValueError("分析时点年份无效")
    if "yearly" not in model:
        return
    yearly = model["yearly"]
    if not isinstance(yearly, list):
        raise ValueError("年度统计结构无效")
    counts, denominators = defaultdict(Counter), {}
    for row in yearly:
        if not isinstance(row, dict) or any(
            type(row.get(k)) is not int for k in ("year", "topic", "count", "denominator")
        ):
            raise ValueError("年度统计年份、主题和计数必须为整数")
        year, topic, count, denominator = (row[k] for k in ("year", "topic", "count", "denominator"))
        if (
            not 1 <= year <= 9999
            or topic not in ids | {-1}
            or count < 1
            or denominator < count
            or topic in counts[year]
            or denominators.get(year, denominator) != denominator
        ):
            raise ValueError("年度统计计数或主题索引无效")
        counts[year][topic], denominators[year] = count, denominator
    if any(sum(values.values()) != denominators[year] for year, values in counts.items()):
        raise ValueError("年度分母必须包含全部记录及离群")
    if all("year" in d for d in documents):
        expected = defaultdict(Counter)
        by_key = {d["document_key"]: d for d in documents}
        for row in assignments:
            year = by_key[row["document_key"]]["year"]
            if year is not None:
                expected[year][row["topic"]] += 1
        if dict(counts) != dict(expected):
            raise ValueError("年度统计与冻结语料及逐篇归属不一致")
