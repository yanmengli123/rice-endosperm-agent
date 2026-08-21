from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.knowledge.graphs.graph_utils import normalize_entity_name
from yuxi.storage.postgres.models_knowledge import KnowledgeGraphEntity, KnowledgeGraphEntityAlias

ENTITY_RESOLVER_VERSION = "1.1"

_DOMAIN_MENTION_ALIASES = {
    "水稻胚乳发育": "endosperm development",
    "胚乳发育": "endosperm development",
}


def _normalized_exact_candidates(mention: str) -> list[str]:
    normalized = normalize_entity_name(mention)
    mapped = _DOMAIN_MENTION_ALIASES.get(normalized)
    return [normalized, mapped] if mapped and mapped != normalized else [normalized]


def _serialize(entity: KnowledgeGraphEntity, *, mention: str, tier: str) -> dict[str, Any]:
    return {
        "mention": mention,
        "entity_id": entity.entity_id,
        "kb_id": entity.kb_id,
        "canonical_identity": entity.canonical_identity,
        "canonical_name": entity.name,
        "normalized_name": entity.normalized_name,
        "label": entity.label,
        "match_tier": tier,
    }


async def resolve_entities(
    db: AsyncSession,
    *,
    mention: str,
    kb_ids: list[str],
    allow_lexical_fallback: bool = True,
) -> dict[str, Any]:
    normalized = normalize_entity_name(mention)
    if not normalized or not kb_ids:
        return {
            "mention": mention,
            "match_tier": "NO_MATCH",
            "ambiguity": False,
            "entities": [],
            "resolver_version": ENTITY_RESOLVER_VERSION,
        }

    exact_candidates = _normalized_exact_candidates(mention)
    exact = list(
        (
            await db.execute(
                select(KnowledgeGraphEntity).where(
                    KnowledgeGraphEntity.kb_id.in_(kb_ids),
                    KnowledgeGraphEntity.normalized_name.in_(exact_candidates),
                )
            )
        )
        .scalars()
        .all()
    )
    tier = "EXACT_CANONICAL"

    if not exact:
        aliases = list(
            (
                await db.execute(
                    select(KnowledgeGraphEntity)
                    .join(
                        KnowledgeGraphEntityAlias,
                        KnowledgeGraphEntityAlias.entity_id == KnowledgeGraphEntity.entity_id,
                    )
                    .where(
                        KnowledgeGraphEntityAlias.kb_id.in_(kb_ids),
                        KnowledgeGraphEntityAlias.normalized_alias.in_(exact_candidates),
                    )
                )
            )
            .scalars()
            .all()
        )
        exact = aliases
        tier = "EXACT_ALIAS"

    if not exact and allow_lexical_fallback:
        exact = list(
            (
                await db.execute(
                    select(KnowledgeGraphEntity)
                    .where(
                        KnowledgeGraphEntity.kb_id.in_(kb_ids),
                        KnowledgeGraphEntity.normalized_name.ilike(f"%{normalized}%"),
                    )
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        tier = "PHRASE_LEXICAL" if exact else "NO_MATCH"

    exact.sort(
        key=lambda entity: (
            0 if "phenotype" in str(entity.label).casefold() else 1,
            str(entity.kb_id),
            str(entity.entity_id),
        )
    )
    # 同一术语可能因历史导入保留 Phenotype/PHENOTYPE/表型等标签变体；
    # exact normalized_name 相同不构成语义歧义，检索层会用可引用证据选择权威节点。
    identities = {entity.normalized_name for entity in exact}
    return {
        "mention": mention,
        "match_tier": tier,
        "ambiguity": len(identities) > 1,
        "entities": [_serialize(entity, mention=mention, tier=tier) for entity in exact],
        "resolver_version": ENTITY_RESOLVER_VERSION,
    }
