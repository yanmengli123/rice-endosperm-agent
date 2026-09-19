from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from yuxi.knowledge.graphs.graph_utils import normalize_entity_name


class GraphExtractor(ABC):
    extractor_type: str

    def __init__(self, options: dict[str, Any] | None = None):
        self.options = options or {}

    @abstractmethod
    async def extract(self, text: str, *, chunk_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        pass

    def validate_options(self) -> None:
        return None

    def extraction_fingerprint(self, *, doclex_fingerprint: str | None = None) -> str | None:
        """抽取配置指纹（D3 缓存失效）：返回 None 表示该抽取器不做指纹失效（保持旧行为）。"""
        return None


def normalize_extraction_result(result: dict[str, Any], extractor_type: str) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError("extraction_result 必须是对象")

    entities = result.get("entities") or []
    relations = result.get("relations") or []
    if not isinstance(entities, list) or not isinstance(relations, list):
        raise ValueError("extraction_result.entities 和 relations 必须是数组")

    normalized_entities_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    entity_refs: dict[str, dict[str, Any]] = {}

    def add_entity(entity: Any, path: str) -> dict[str, Any]:
        normalized_entity = _normalize_entity(entity, path)
        key = _entity_key(normalized_entity)
        existing = normalized_entities_by_key.get(key)
        if existing is None:
            normalized_entities_by_key[key] = normalized_entity
            existing = normalized_entity
        else:
            _merge_attributes(existing, normalized_entity)

        for ref in _entity_refs(entity, existing):
            entity_refs[ref] = existing
        return existing

    for index, entity in enumerate(entities):
        add_entity(entity, f"entities[{index}]")

    normalized_relations = []
    for index, relation in enumerate(relations):
        if not isinstance(relation, dict):
            raise ValueError("relations 元素必须是对象")
        source = _normalize_relation_endpoint(
            relation.get("source"),
            entity_refs,
            add_entity,
            result,
            f"relations[{index}].source",
        )
        target = _normalize_relation_endpoint(
            relation.get("target"),
            entity_refs,
            add_entity,
            result,
            f"relations[{index}].target",
        )
        text = str(relation.get("text") or "").strip()
        if not text:
            raise ValueError("relations[].text 不能为空")
        normalized_relation = {
            "source": source,
            "target": target,
            "text": text,
            "label": str(relation.get("label") or "RELATED_TO").strip() or "RELATED_TO",
        }
        # 科研抽取器附带的关系级证据属性：缺省不出现，保持通用抽取器输出形状不变
        if relation.get("confidence") is not None:
            normalized_relation["confidence"] = min(1.0, max(0.0, float(relation["confidence"])))
        if relation.get("hedge") is not None:
            normalized_relation["hedge"] = bool(relation["hedge"])
        if isinstance(relation.get("context"), dict) and relation["context"]:
            normalized_relation["context"] = {
                str(key): str(value) for key, value in relation["context"].items() if str(value).strip()
            }
        if relation.get("trigger_verified") is not None:
            normalized_relation["trigger_verified"] = bool(relation["trigger_verified"])
        if relation.get("trigger_term"):
            normalized_relation["trigger_term"] = str(relation["trigger_term"])
        if relation.get("verifier_confirmed") is not None:
            normalized_relation["verifier_confirmed"] = bool(relation["verifier_confirmed"])
        normalized_relations.append(normalized_relation)

    metadata = dict(result.get("metadata") or {})
    metadata.setdefault("extractor_type", extractor_type)
    metadata.setdefault("schema_version", 1)
    return {
        "entities": list(normalized_entities_by_key.values()),
        "relations": normalized_relations,
        "metadata": metadata,
    }


def _normalize_relation_endpoint(
    endpoint: Any,
    entity_refs: dict[str, dict[str, Any]],
    add_entity: Callable[[Any, str], dict[str, Any]],
    result: dict[str, Any],
    path: str,
) -> dict[str, Any]:
    if isinstance(endpoint, dict):
        return add_entity(endpoint, path)

    endpoint_ref = str(endpoint or "").strip()
    entity = entity_refs.get(endpoint_ref)
    if entity is None:
        raise ValueError(
            f"relations[].source/target 必须是实体对象，或引用 entities[].text/id，"
            f"未找到: {path}={endpoint_ref}, Result: {result}"
        )
    return entity


def _normalize_entity(entity: Any, path: str) -> dict[str, Any]:
    if not isinstance(entity, dict):
        raise ValueError(f"{path} 必须是对象")

    text = str(entity.get("text") or "").strip()
    if not text:
        raise ValueError(f"{path}.text 不能为空")

    attributes = entity.get("attributes") or []
    if not isinstance(attributes, list):
        raise ValueError(f"{path}.attributes 必须是数组")

    normalized_attributes = []
    for attribute in attributes:
        if not isinstance(attribute, dict):
            raise ValueError(f"{path}.attributes 元素必须是对象")
        attr_text = str(attribute.get("text") or "").strip()
        if not attr_text:
            continue
        normalized_attributes.append(
            {
                "text": attr_text,
                "label": str(attribute.get("label") or "Attribute").strip() or "Attribute",
            }
        )

    normalized = {
        "text": text,
        "label": str(entity.get("label") or "Entity").strip() or "Entity",
        "attributes": normalized_attributes,
    }
    aliases = entity.get("aliases")
    if isinstance(aliases, list):
        seen = {text}
        normalized_aliases = []
        for alias in aliases:
            alias_text = str(alias or "").strip()
            if alias_text and alias_text not in seen:
                seen.add(alias_text)
                normalized_aliases.append(alias_text)
        if normalized_aliases:
            normalized["aliases"] = normalized_aliases
    mention_quote = str(entity.get("mention_quote") or "").strip()
    if mention_quote:
        normalized["mention_quote"] = mention_quote
    return normalized


def _entity_key(entity: dict[str, Any]) -> tuple[str, str]:
    return (normalize_entity_name(entity["text"]), entity["label"])


def _entity_refs(raw_entity: Any, entity: dict[str, Any]) -> list[str]:
    refs = [entity["text"]]
    if isinstance(raw_entity, dict):
        entity_id = str(raw_entity.get("id") or "").strip()
        if entity_id:
            refs.append(entity_id)
    return refs


def _merge_attributes(target: dict[str, Any], source: dict[str, Any]) -> None:
    known_attributes = {(attr["text"], attr["label"]) for attr in target.get("attributes") or []}
    for attribute in source.get("attributes") or []:
        attribute_key = (attribute["text"], attribute["label"])
        if attribute_key not in known_attributes:
            target.setdefault("attributes", []).append(attribute)
            known_attributes.add(attribute_key)
    source_aliases = source.get("aliases") or []
    if source_aliases:
        known_aliases = {target["text"], *(target.get("aliases") or [])}
        for alias in source_aliases:
            if alias not in known_aliases:
                target.setdefault("aliases", []).append(alias)
                known_aliases.add(alias)
    if not target.get("mention_quote") and source.get("mention_quote"):
        target["mention_quote"] = source["mention_quote"]
