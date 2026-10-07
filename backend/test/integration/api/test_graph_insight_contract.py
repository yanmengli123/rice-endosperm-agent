"""真实 Neo4j → HTTP 子图契约，无 LLM 调用，仅创建 fixture 知识库投影。"""

import asyncio
import time
import uuid

import pytest
from sqlalchemy import delete, select, update

from yuxi.storage.neo4j import get_shared_neo4j_connection, neo4j_write, safe_neo4j_label
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Tenant, TenantMembership

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_databases():
    """覆盖父级按名称扫描删除；knowledge_database 仅按本次创建的 kb_id 清理。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    """图谱测试不创建沙箱，禁止父级清理影响其他正在运行的任务。"""
    yield


async def test_canvas_modes_share_distinct_fact_and_role_contract(test_client, admin_headers, knowledge_database):
    kb_id = knowledge_database["kb_id"]
    label = safe_neo4j_label(kb_id)
    driver = get_shared_neo4j_connection().driver

    for full_graph in (False, True):
        empty_response = await test_client.get(
            "/api/graph/subgraph",
            headers=admin_headers,
            params={"kb_id": kb_id, "full_graph": full_graph},
        )
        assert empty_response.status_code == 200
        assert empty_response.json()["data"]["truncated"] is False
        assert empty_response.json()["data"]["truncation_reasons"] == []

    def seed(tx):
        tx.run(
            f"""
            CREATE (a:Entity:MilvusKB:`{label}` {{kb_id: $kb_id, entity_id: 'a', name: 'Gene A', label: 'Gene'}})
            CREATE (b:Entity:MilvusKB:`{label}` {{kb_id: $kb_id, entity_id: 'b', name: 'Trait B', label: 'Phenotype'}})
            CREATE (c:Entity:MilvusKB:`{label}` {{kb_id: $kb_id, entity_id: 'c',
                    name: 'Reserved name', label: 'Chunk'}})
            WITH a, b, c
            UNWIND range(1, 3) AS i
            CREATE (a)-[:RELATION {{kb_id: $kb_id, triple_id: 't1', type: 'ACTIVATES',
                    chunk_id: toString(i), review_status: 'APPROVED'}}]->(b)
            WITH DISTINCT a, b, c
            CREATE (b)-[:RELATION {{kb_id: $kb_id, triple_id: 't2', type: 'INHIBITS', review_status: 'CANDIDATE'}}]->(a)
            CREATE (a)-[:RELATION {{kb_id: $kb_id, triple_id: 't3', type: 'MENTIONS', review_status: 'APPROVED'}}]->(c)
            """,
            kb_id=kb_id,
        ).consume()

    await asyncio.to_thread(neo4j_write, driver, seed)
    for policy, expected in (("candidates_visible", {"t1", "t2", "t3"}), ("approved_only", {"t1", "t3"})):
        for full_graph in (False, True):
            response = await test_client.get(
                "/api/graph/subgraph",
                headers=admin_headers,
                params={
                    "kb_id": kb_id,
                    "max_nodes": 100,
                    "max_depth": 2,
                    "exclude_chunk": True,
                    "full_graph": full_graph,
                    "review_policy": policy,
                },
            )
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            assert {edge["triple_id"] for edge in data["edges"]} == expected
            assert len(data["edges"]) == len(expected)
            assert data["truncated"] is False
            assert data["meta"]["metric"] == "distinct_triples"
            assert data["meta"]["review_policy"] == policy
            assert data["meta"]["generated_at"].endswith("Z")
            assert all(node["node_kind"] == "entity" for node in data["nodes"])
            assert all(edge["edge_kind"] == "relation" for edge in data["edges"])
            assert any(node["type"] == "Chunk" for node in data["nodes"])
            ids = {node["id"] for node in data["nodes"]}
            assert all(edge["source_id"] in ids and edge["target_id"] in ids for edge in data["edges"])

    bounded = await test_client.get(
        "/api/graph/subgraph",
        headers=admin_headers,
        params={"kb_id": kb_id, "max_nodes": 1, "max_depth": 2, "exclude_chunk": True},
    )
    assert bounded.status_code == 200
    bounded_data = bounded.json()["data"]
    assert bounded_data["truncated"] is True
    assert {"node_limit", "seed_limit"} <= set(bounded_data["truncation_reasons"])
    assert len(bounded_data["nodes"]) == 1


async def test_subgraph_and_full_graph_enforce_knowledge_access(test_client, knowledge_database):
    for full_graph in (False, True):
        response = await test_client.get(
            "/api/graph/subgraph", params={"kb_id": knowledge_database["kb_id"], "full_graph": full_graph}
        )
        assert response.status_code == 401


async def test_three_view_snapshot_preserves_internal_isolated_and_directional_facts(
    test_client, admin_headers, knowledge_database
):
    kb_id = knowledge_database["kb_id"]
    label = safe_neo4j_label(kb_id)
    driver = get_shared_neo4j_connection().driver

    def seed(tx):
        tx.run(
            f"""
            CREATE (a:Entity:MilvusKB:`{label}` {{kb_id: $kb_id, entity_id: 'a', name: 'Gene A', label: 'Gene'}})
            CREATE (b:Entity:MilvusKB:`{label}` {{kb_id: $kb_id, entity_id: 'b', name: 'Trait B', label: 'Phenotype'}})
            CREATE (c:Entity:MilvusKB:`{label}` {{kb_id: $kb_id, entity_id: 'c', name: 'Gene C', label: 'Gene'}})
            CREATE (d:Entity:MilvusKB:`{label}` {{kb_id: $kb_id, entity_id: 'd', name: 'Special D', label: $special}})
            CREATE (e:Entity:MilvusKB:`{label}` {{kb_id: $kb_id, entity_id: 'e',
                    name: 'Isolated E', label: 'Isolated'}})
            CREATE (a)-[:RELATION {{kb_id: $kb_id, triple_id: 'qa', type: 'ACTIVATES',
                    chunk_id: '1', review_status: 'APPROVED'}}]->(b)
            CREATE (a)-[:RELATION {{kb_id: $kb_id, triple_id: 'qa', type: 'ACTIVATES',
                    chunk_id: '2', review_status: 'APPROVED'}}]->(b)
            CREATE (b)-[:RELATION {{kb_id: $kb_id, triple_id: 'qb', type: 'INHIBITS', review_status: 'CANDIDATE'}}]->(a)
            CREATE (a)-[:RELATION {{kb_id: $kb_id, triple_id: 'qc', type: 'REGULATES', review_status: 'APPROVED'}}]->(c)
            CREATE (c)-[:RELATION {{kb_id: $kb_id, triple_id: 'qd', type: $predicate, review_status: 'APPROVED'}}]->(d)
            """,
            kb_id=kb_id,
            special="Gene\u200b",
            predicate="RELATED\u200b",
        ).consume()

    await asyncio.to_thread(neo4j_write, driver, seed)
    for policy, expected in (("candidates_visible", {"qa", "qb", "qc", "qd"}), ("approved_only", {"qa", "qc", "qd"})):
        for full_graph in (False, True):
            response = await test_client.get(
                "/api/graph/subgraph",
                headers=admin_headers,
                params={
                    "kb_id": kb_id,
                    "max_nodes": 100,
                    "max_depth": 2,
                    "exclude_chunk": True,
                    "full_graph": full_graph,
                    "review_policy": policy,
                },
            )
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            nodes = {node["name"]: node for node in data["nodes"]}
            assert len(nodes) == 5
            assert nodes["Isolated E"]["type"] == "Isolated"
            assert nodes["Special D"]["type"] == "Gene\u200b"
            edges = {edge["triple_id"]: edge for edge in data["edges"]}
            assert len(data["edges"]) == len(expected)
            assert set(edges) == expected
            assert edges["qc"]["source_id"] == nodes["Gene A"]["id"]
            assert edges["qc"]["target_id"] == nodes["Gene C"]["id"]
            assert edges["qd"]["type"] == "RELATED\u200b"
            assert edges["qa"]["source_id"] == nodes["Gene A"]["id"]
            if "qb" in edges:
                assert edges["qb"]["source_id"] == nodes["Trait B"]["id"]
                assert edges["qb"]["target_id"] == nodes["Gene A"]["id"]
            assert not data["truncated"]
            assert data["meta"]["review_policy"] == policy
            assert data["meta"]["metric"] == "distinct_triples"


async def test_member_access_is_tenant_scoped_and_revocable(
    test_client, admin_headers, knowledge_database, standard_user
):
    kb_id = knowledge_database["kb_id"]
    uid = standard_user["user"]["uid"]
    headers = standard_user["headers"]

    async def assert_unavailable():
        for params in ({"full_graph": False}, {"full_graph": True}):
            response = await test_client.get("/api/graph/subgraph", headers=headers, params={"kb_id": kb_id, **params})
            assert response.status_code == 404
            assert "data" not in response.json()
        response = await test_client.get(
            "/api/graph/evidence/triple", headers=headers, params={"kb_id": kb_id, "triple_id": "private"}
        )
        assert response.status_code == 404

    # 显式私有共享策略，协作授权是唯一放行路径。
    response = await test_client.put(
        f"/api/knowledge/databases/{kb_id}",
        headers=admin_headers,
        json={
            "name": f"three-view-private-{uuid.uuid4().hex}",
            "description": "Private fixture",
            "share_config": {
                "access_level": "user",
                "user_uids": [knowledge_database.get("created_by") or "fixture-owner"],
            },
        },
    )
    assert response.status_code == 200, response.text
    await assert_unavailable()
    granted = await test_client.put(
        "/api/graph/governance/members",
        headers=admin_headers,
        json={"kb_id": kb_id, "uid": uid, "capability": "viewer"},
    )
    assert granted.status_code == 200, granted.text
    for full_graph in (False, True):
        response = await test_client.get(
            "/api/graph/subgraph", headers=headers, params={"kb_id": kb_id, "full_graph": full_graph}
        )
        assert response.status_code == 200, response.text
    revoked = await test_client.delete(
        "/api/graph/governance/members", headers=admin_headers, params={"kb_id": kb_id, "uid": uid}
    )
    assert revoked.status_code == 200
    await assert_unavailable()

    # 只移动本测试创建的账号；不触碰其他用户或生产租户。
    foreign_tenant_id = None
    original_tenant_id = None
    try:
        async with pg_manager.get_async_session_context() as session:
            membership = (
                await session.execute(select(TenantMembership).where(TenantMembership.uid == uid))
            ).scalar_one()
            original_tenant_id = membership.tenant_id
            tenant = Tenant(name=f"three_view_fixture_{uuid.uuid4().hex}", status="active")
            session.add(tenant)
            await session.flush()
            foreign_tenant_id = tenant.id
            membership.tenant_id = foreign_tenant_id
        rejected = await test_client.put(
            "/api/graph/governance/members",
            headers=admin_headers,
            json={"kb_id": kb_id, "uid": uid, "capability": "publisher"},
        )
        assert rejected.status_code == 400
        await assert_unavailable()
        # query/body 目标分歧，必须在处理器写入前拒绝。
        spoofed = await test_client.put(
            "/api/graph/governance/members",
            headers=admin_headers,
            params={"kb_id": "different_fixture"},
            json={"kb_id": kb_id, "uid": uid, "capability": "publisher"},
        )
        assert spoofed.status_code == 400
    finally:
        if foreign_tenant_id is not None:
            async with pg_manager.get_async_session_context() as session:
                await session.execute(
                    update(TenantMembership).where(TenantMembership.uid == uid).values(tenant_id=original_tenant_id)
                )
                await session.execute(delete(Tenant).where(Tenant.id == foreign_tenant_id))


async def test_concurrent_full_reads_keep_closed_fact_sets(test_client, admin_headers, knowledge_database):
    kb_id = knowledge_database["kb_id"]
    label = safe_neo4j_label(kb_id)
    driver = get_shared_neo4j_connection().driver

    def replace(tx, generation):
        tx.run(f"MATCH (n:MilvusKB:`{label}`) DETACH DELETE n").consume()
        tx.run(
            f"""CREATE (a:Entity:MilvusKB:`{label}` {{name: 'A', label: 'Gene', entity_id: $entity}})
            CREATE (b:Entity:MilvusKB:`{label}` {{name: 'B', label: 'Trait', entity_id: $entity}})
            CREATE (a)-[:RELATION {{type: 'REGULATES', triple_id: $triple, review_status: 'APPROVED'}}]->(b)""",
            entity=f"entity-{generation}",
            triple=f"triple-{generation}",
        ).consume()

    await asyncio.to_thread(neo4j_write, driver, lambda tx: replace(tx, 0))
    seen_facts = set()

    async def writer():
        for generation in range(1, 16):
            await asyncio.to_thread(neo4j_write, driver, lambda tx: replace(tx, generation))
            await asyncio.sleep(0.01)

    async def reader():
        for _ in range(15):
            response = await test_client.get(
                "/api/graph/subgraph",
                headers=admin_headers,
                params={"kb_id": kb_id, "full_graph": True, "exclude_chunk": True, "review_policy": "approved_only"},
            )
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            identifiers = {node["id"] for node in data["nodes"]}
            facts = [edge["triple_id"] for edge in data["edges"]]
            assert len(facts) == len(set(facts))
            assert all(edge["source_id"] in identifiers and edge["target_id"] in identifiers for edge in data["edges"])
            nodes_by_id = {node["id"]: node for node in data["nodes"]}
            for edge in data["edges"]:
                generation = edge["triple_id"].removeprefix("triple-")
                for endpoint in (edge["source_id"], edge["target_id"]):
                    assert nodes_by_id[endpoint]["properties"]["entity_id"] == f"entity-{generation}"
            if data["truncated"]:
                assert data["truncation_reasons"] == ["concurrent_update"]
            seen_facts.update(facts)
            assert data["meta"]["review_policy"] == "approved_only"

    await asyncio.gather(writer(), *(reader() for _ in range(4)))
    assert seen_facts, "并发查询必须返回真实事实，不能以空集合通过对账"
    stable = await test_client.get(
        "/api/graph/subgraph",
        headers=admin_headers,
        params={"kb_id": kb_id, "full_graph": True, "exclude_chunk": True, "review_policy": "approved_only"},
    )
    assert stable.status_code == 200, stable.text
    stable_data = stable.json()["data"]
    assert len(stable_data["nodes"]) == 2
    assert [edge["triple_id"] for edge in stable_data["edges"]] == ["triple-15"]
    assert not stable_data["truncated"]


async def test_full_query_budget_load(test_client, admin_headers, knowledge_database, record_property):
    """预算边界真实 HTTP 负载。阈值只约束本地验收环境，不推定生产 SLA。"""
    kb_id = knowledge_database["kb_id"]
    label = safe_neo4j_label(kb_id)
    driver = get_shared_neo4j_connection().driver

    def seed(tx):
        tx.run(
            f"""UNWIND range(0, 2999) AS i
            CREATE (:Entity:MilvusKB:`{label}` {{entity_id: toString(i), name: 'Entity ' + toString(i),
                label: 'Type ' + toString(i % 60)}})"""
        ).consume()
        tx.run(
            f"""MATCH (n:MilvusKB:`{label}`) WITH n ORDER BY toInteger(n.entity_id)
            WITH collect(n) AS nodes UNWIND range(0, 5999) AS i
            WITH nodes[i % 3000] AS a, nodes[(i + 1) % 3000] AS b, i
            CREATE (a)-[:RELATION {{triple_id: 'fact-' + toString(i), type: 'REGULATES',
                review_status: 'APPROVED'}}]->(b)"""
        ).consume()

    await asyncio.to_thread(neo4j_write, driver, seed)
    durations = []

    async def reader():
        for _ in range(10):
            start = time.perf_counter()
            response = await test_client.get(
                "/api/graph/subgraph",
                headers=admin_headers,
                params={"kb_id": kb_id, "full_graph": True, "exclude_chunk": True},
            )
            durations.append(time.perf_counter() - start)
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            assert len(data["nodes"]) == 3000
            assert len(data["edges"]) == 6000
            assert not data["truncated"]
            assert len({edge["triple_id"] for edge in data["edges"]}) == 6000

    await asyncio.gather(*(reader() for _ in range(4)))
    p95 = sorted(durations)[int(len(durations) * 0.95) - 1]
    record_property("graph_http_requests", len(durations))
    record_property("graph_http_concurrency", 4)
    record_property("graph_http_p95_seconds", round(p95, 3))
    record_property("graph_http_max_seconds", round(max(durations), 3))
    assert p95 < 10, f"预算边界本地 p95 超过10秒: {p95:.3f}s"

    def add_source_duplicate(tx):
        tx.run(
            f"""MATCH (a:MilvusKB:`{label}`)-[r]->(b:MilvusKB:`{label}`)
            WHERE r.triple_id = 'fact-0'
            CREATE (a)-[:RELATION {{triple_id: 'fact-0', type: 'REGULATES',
                review_status: 'APPROVED', chunk_id: 'additional-source'}}]->(b)"""
        ).consume()

    await asyncio.to_thread(neo4j_write, driver, add_source_duplicate)
    overflow = await test_client.get(
        "/api/graph/subgraph",
        headers=admin_headers,
        params={"kb_id": kb_id, "full_graph": True, "exclude_chunk": True},
    )
    assert overflow.status_code == 200, overflow.text
    data = overflow.json()["data"]
    assert len(data["nodes"]) == 3000
    assert len(data["edges"]) == 6000
    assert data["truncated"] is True
    assert data["truncation_reasons"] == ["edge_safety_cap"]

    await asyncio.to_thread(
        neo4j_write,
        driver,
        lambda tx: tx.run(f"CREATE (:Entity:MilvusKB:`{label}` {{name: 'Overflow', label: 'Isolated'}})").consume(),
    )
    overflow = await test_client.get(
        "/api/graph/subgraph",
        headers=admin_headers,
        params={"kb_id": kb_id, "full_graph": True, "exclude_chunk": True},
    )
    assert overflow.status_code == 200, overflow.text
    data = overflow.json()["data"]
    assert len(data["nodes"]) == 3000
    assert len(data["edges"]) <= 6000
    identifiers = {node["id"] for node in data["nodes"]}
    assert all(edge["source_id"] in identifiers and edge["target_id"] in identifiers for edge in data["edges"])
    assert data["truncated"] is True
    assert {"node_safety_cap", "edge_safety_cap"} <= set(data["truncation_reasons"])
    record_property("graph_source_edge_overflow_verified", True)
    record_property("graph_node_overflow_verified", True)
