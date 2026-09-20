"""源资产目录：图谱导入按 role 展开与文件元数据保留。"""

from yuxi.knowledge.graphs.source_asset_catalog import build_graph_import_asset_rows


def test_build_graph_import_asset_rows_expands_by_role():
    rows = build_graph_import_asset_rows(
        tenant_id=1,
        kb_id="kb22",
        import_id="gimp_test",
        contract_ref="managed_graph@1.0.0",
        checksums={"nodes": "n" * 64, "relationships": "r" * 64, "audit": "a" * 64},
        object_keys={
            "nodes": "kb22/graph-imports/gimp_test/n_nodes.csv",
            "relationships": "kb22/graph-imports/gimp_test/r_relationships.csv",
            "audit": "kb22/graph-imports/gimp_test/a_description.cypher",
        },
        file_metas={
            "nodes": {"filename": "节点.csv", "content_type": "text/csv", "size": 12345},
            "relationships": {"filename": "关系.csv", "content_type": "text/csv", "size": 67890},
            "audit": {"filename": "说明.cypher", "content_type": "text/plain", "size": 100},
        },
        created_by="user-1",
    )

    assert [row["role"] for row in rows] == ["nodes", "relationships", "audit"]
    assert [row["asset_kind"] for row in rows] == ["graph_nodes", "graph_relationships", "graph_audit"]
    assert rows[0]["original_filename"] == "节点.csv"
    assert rows[0]["size_bytes"] == 12345
    assert rows[2]["content_type"] == "text/plain"
    # 同批次同 role 的 asset_id 确定性生成（幂等 upsert 依赖）
    again = build_graph_import_asset_rows(
        tenant_id=1,
        kb_id="kb22",
        import_id="gimp_test",
        contract_ref="managed_graph@1.0.0",
        checksums={"nodes": "n" * 64, "relationships": "r" * 64, "audit": "a" * 64},
        object_keys={
            "nodes": "kb22/graph-imports/gimp_test/n_nodes.csv",
            "relationships": "kb22/graph-imports/gimp_test/r_relationships.csv",
            "audit": "kb22/graph-imports/gimp_test/a_description.cypher",
        },
    )
    assert again[0]["asset_id"] == rows[0]["asset_id"]


def test_build_graph_import_asset_rows_skips_missing_audit():
    rows = build_graph_import_asset_rows(
        tenant_id=1,
        kb_id="kb22",
        import_id="gimp_test",
        contract_ref="managed_graph@1.0.0",
        checksums={"nodes": "n" * 64, "relationships": "r" * 64, "audit": None},
        object_keys={
            "nodes": "kb22/graph-imports/gimp_test/n_nodes.csv",
            "relationships": "kb22/graph-imports/gimp_test/r_relationships.csv",
            "audit": None,
        },
    )

    assert [row["role"] for row in rows] == ["nodes", "relationships"]
    # 未携带文件元数据时用 role 兜底命名（新上传路径始终传 file_metas）
    assert rows[0]["original_filename"] == "graph-import · nodes"
    assert rows[0]["size_bytes"] is None
