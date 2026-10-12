"""文献计量聚合（G3）：只读快照 + 纯聚合。

快照口径与 ``topics._snapshot`` 一致：``paper_visibility``（租户 + 回收站 +
ACL 投影）+ ``is_reference=False``（排除 PaperAgent 参考条目，参考条目会把
年度趋势/期刊分布完全打歪）+ 50000 篇安全预算失败关闭。聚合函数全部纯函数
（可脱离 DB 单测）；共现网络（关键词/作者）由同篇共现对构建，节点按频次
TopN 截断、边按 min_pair 过滤并设上限防止超大负载。
"""

from __future__ import annotations

import re
from collections import Counter
from itertools import combinations
from typing import Any

from sqlalchemy import select

from yuxi.literature.access import current_access, paper_visibility
from yuxi.literature.metrics import normalize_journal_name
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteraturePaper

MAX_BIBLIOMETRIC_PAPERS = 50_000
MAX_NETWORK_LINKS = 2_000
MAX_NETWORK_PAIR_OPERATIONS = 2_000_000


async def _snapshot(tenant_id: int, **filters) -> list[dict[str, Any]]:
    """计量输入快照：当前范围内可见主文献（排除参考条目/回收站），50000 篇预算。"""
    scope = current_access()
    if scope is None or scope.tenant_id != tenant_id:
        raise PermissionError("文献计量需要当前用户访问上下文")
    async with pg_manager.get_async_session_context() as session:
        from yuxi.literature.reference_batches import record_conditions

        condition = await record_conditions(session, tenant_id, **filters) if filters else paper_visibility(tenant_id)
        rows = (
            await session.execute(
                select(
                    LiteraturePaper.kind,
                    LiteraturePaper.language,
                    LiteraturePaper.pub_year,
                    LiteraturePaper.journal,
                    LiteraturePaper.authors_json,
                    LiteraturePaper.keywords_json,
                    LiteraturePaper.affiliations_json,
                )
                .where(condition, LiteraturePaper.is_reference.is_(False))
                .order_by(LiteraturePaper.rid)
                .limit(MAX_BIBLIOMETRIC_PAPERS + 1)
            )
        ).all()
    if len(rows) > MAX_BIBLIOMETRIC_PAPERS:
        raise ValueError(f"文献计量聚合超过 {MAX_BIBLIOMETRIC_PAPERS} 篇安全预算")
    return [
        {
            "kind": kind or "paper",
            "language": language or "",
            "pub_year": pub_year,
            "journal": (journal or "").strip(),
            "authors": [value for value in (authors or []) if isinstance(value, str) and value.strip()],
            "keywords": [value for value in (keywords or []) if isinstance(value, str) and value.strip()],
            "affiliations": [value for value in (affiliations or []) if isinstance(value, str) and value.strip()],
        }
        for kind, language, pub_year, journal, authors, keywords, affiliations in rows
    ]


# ---------------------------------------------------------------- 纯聚合


def _keyword_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _author_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def aggregate_overview(papers: list[dict[str, Any]]) -> dict[str, Any]:
    years = [paper["pub_year"] for paper in papers if paper["pub_year"]]
    return {
        "papers": len(papers),
        "year_min": min(years) if years else None,
        "year_max": max(years) if years else None,
        "journals": len({normalize_journal_name(paper["journal"]) for paper in papers if paper["journal"]}),
        "authors": len({_author_key(value) for paper in papers for value in paper["authors"]}),
        "keywords": len({_keyword_key(value) for paper in papers for value in paper["keywords"]}),
        "by_kind": dict(sorted(Counter(paper["kind"] for paper in papers).items())),
        "by_language": dict(sorted(Counter(paper["language"] or "unknown" for paper in papers).items())),
        "missing_year": sum(not paper["pub_year"] for paper in papers),
    }


def aggregate_yearly(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """年度趋势：pub_year 是 Integer 列，GROUP BY 语义 + 区间补零。"""
    by_year: dict[int, Counter[str]] = {}
    for paper in papers:
        year = paper["pub_year"]
        if not year:
            continue
        by_year.setdefault(year, Counter())[paper["language"] or "unknown"] += 1
    if not by_year:
        return []
    return [
        {
            "year": year,
            "count": sum(by_year.get(year, Counter()).values()),
            "by_language": dict(sorted(by_year.get(year, Counter()).items())),
        }
        for year in range(min(by_year), max(by_year) + 1)
    ]


def aggregate_journals(papers: list[dict[str, Any]], *, top: int = 20) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    display: dict[str, str] = {}
    for paper in papers:
        if not paper["journal"]:
            continue
        key = normalize_journal_name(paper["journal"])
        counter[key] += 1
        display.setdefault(key, paper["journal"])
    return [{"name": display[key], "normalized": key, "count": count} for key, count in counter.most_common(top)]


#: WoS 地址尾段的常见国家写法归一（"Peoples R China" 等）。
_WOS_COUNTRY_ALIASES = {
    "peoples r china": "China",
    "p r china": "China",
    "china": "China",
    "usa": "USA",
    "u s a": "USA",
    "united states": "USA",
    "england": "UK",
    "scotland": "UK",
    "wales": "UK",
    "north ireland": "UK",
    "uk": "UK",
    "united kingdom": "UK",
    "中国": "China",
    "美国": "USA",
    "south korea": "South Korea",
    "republic of korea": "South Korea",
    "korea": "South Korea",
    "russian federation": "Russia",
}
_WOS_COUNTRY_ALIASES.update(
    {
        name.casefold(): name
        for name in (
            "Japan",
            "Germany",
            "France",
            "Italy",
            "Spain",
            "Portugal",
            "Netherlands",
            "Belgium",
            "Switzerland",
            "Austria",
            "Denmark",
            "Sweden",
            "Norway",
            "Finland",
            "Poland",
            "Greece",
            "Ireland",
            "Iceland",
            "Canada",
            "Australia",
            "New Zealand",
            "India",
            "Pakistan",
            "Bangladesh",
            "Sri Lanka",
            "Nepal",
            "Singapore",
            "Malaysia",
            "Indonesia",
            "Thailand",
            "Vietnam",
            "Philippines",
            "Cambodia",
            "Laos",
            "Brazil",
            "Argentina",
            "Chile",
            "Peru",
            "Colombia",
            "Mexico",
            "South Africa",
            "Egypt",
            "Nigeria",
            "Kenya",
            "Ethiopia",
            "Ghana",
            "Morocco",
            "Tunisia",
            "Algeria",
            "Iran",
            "Iraq",
            "Israel",
            "Turkey",
            "Saudi Arabia",
            "United Arab Emirates",
            "Qatar",
            "Jordan",
            "Lebanon",
            "Russia",
            "Ukraine",
            "Romania",
            "Hungary",
            "Czech Republic",
            "Czechia",
            "Slovakia",
            "Slovenia",
            "Croatia",
            "Serbia",
            "Bulgaria",
            "Estonia",
            "Latvia",
            "Lithuania",
            "Luxembourg",
            "Cyprus",
            "Malta",
            "Taiwan",
            "Hong Kong",
            "Macao",
        )
    }
)


def _affiliation_country(affiliation: str) -> str:
    """只识别明确国家名；城市、邮编、邮箱和未识别尾段绝不猜成国家。"""
    affiliation = re.split(r"\b(?:electronic address|e-?mail)\s*:", affiliation, maxsplit=1, flags=re.I)[0]
    segments = [segment.strip() for segment in re.split(r"[,，]", affiliation) if segment.strip()]
    for segment in reversed(segments):
        text = segment.rstrip(".,;")
        if text and not text.isdigit():
            return _WOS_COUNTRY_ALIASES.get(text.casefold(), "")
    return ""


def aggregate_affiliations(papers: list[dict[str, Any]], *, top: int = 20) -> dict[str, Any]:
    counter: Counter[str] = Counter()
    countries: Counter[str] = Counter()
    unrecognized = 0
    for paper in papers:
        # 国家按篇计数（一篇多机构同国只计一次，文献计量整计数口径）
        paper_countries: set[str] = set()
        for text in sorted({re.sub(r"\s+", " ", value).strip(" .;,") for value in paper["affiliations"]}):
            if not text:
                continue
            counter[text] += 1
            country = _affiliation_country(text)
            if country:
                paper_countries.add(country)
        countries.update(paper_countries)
        if not paper_countries:
            unrecognized += 1
    return {
        "affiliations_top": [
            {"name": name, "count": count, "country": _affiliation_country(name)}
            for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:top]
        ],
        "countries_top": [
            {"name": name, "count": count}
            for name, count in sorted(countries.items(), key=lambda item: (-item[1], item[0]))[:top]
        ],
        "country_unidentified_papers": unrecognized,
    }


def build_cooccurrence_network(
    papers: list[dict[str, Any]], *, field: str, top: int = 50, min_pair: int = 2
) -> dict[str, Any]:
    """共现网络（关键词/作者）：节点=频次 TopN，边=同一篇内共现次数 ≥ min_pair。"""
    if field not in ("keywords", "authors"):
        raise ValueError("共现字段需为 keywords 或 authors")
    key_fn = _keyword_key if field == "keywords" else _author_key
    freq: Counter[str] = Counter()
    display: dict[str, str] = {}
    paper_values: list[set[str]] = []
    for paper in papers:
        values = set()
        for value in paper[field]:
            key = key_fn(value)
            if key:
                values.add(key)
                display.setdefault(key, value.strip())
        freq.update(values)
        paper_values.append(values)
    keep = set(sorted(freq, key=lambda name: (-freq[name], name))[:top])
    # 先截节点再展开组合，避免一篇超长关键词/合作作者列表制造数百万无用边。
    pair_freq: Counter[tuple[str, str]] = Counter()
    operations = 0
    for values in paper_values:
        retained = sorted(values & keep)
        operations += len(retained) * (len(retained) - 1) // 2
        if operations > MAX_NETWORK_PAIR_OPERATIONS:
            raise ValueError("共现网络计算超过安全预算，请减少 TopN 节点数")
        pair_freq.update(combinations(retained, 2))
    links = [
        {"source": left, "target": right, "weight": weight}
        for (left, right), weight in sorted(pair_freq.items(), key=lambda item: (-item[1], item[0]))
        if weight >= min_pair
    ]
    links_total = len(links)
    links = links[:MAX_NETWORK_LINKS]
    nodes = [
        {"id": key, "label": display.get(key, key), "weight": freq[key]}
        for key in sorted(keep, key=lambda name: (-freq[name], name))
    ]
    return {
        "field": field,
        "nodes": nodes,
        "links": links,
        "stats": {
            "nodes": len(nodes),
            "links": len(links),
            "pairs_total": len(pair_freq),
            "pairs_scope": "top_nodes",
            "links_total": links_total,
            "links_truncated": links_total > len(links),
        },
    }


# ---------------------------------------------------------------- 端点装配


async def bibliometrics(
    tenant_id: int, *, journals_top: int = 20, affiliations_top: int = 20, **filters
) -> dict[str, Any]:
    """计量总览端点数据：overview + 年度趋势 + 期刊/机构/国家分布。"""
    papers = await _snapshot(tenant_id, **filters)
    return {
        "overview": aggregate_overview(papers),
        "yearly": aggregate_yearly(papers),
        "journals_top": aggregate_journals(papers, top=journals_top),
        **aggregate_affiliations(papers, top=affiliations_top),
    }


async def metric_network(tenant_id: int, *, field: str, top: int = 50, min_pair: int = 2, **filters) -> dict[str, Any]:
    """共现网络端点数据（keyword/author）。"""
    papers = await _snapshot(tenant_id, **filters)
    return build_cooccurrence_network(papers, field=field, top=top, min_pair=min_pair)
