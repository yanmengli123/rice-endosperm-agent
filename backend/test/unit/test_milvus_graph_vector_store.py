from yuxi.knowledge.graphs import milvus_graph_vector_store as vector_store_module
from yuxi.knowledge.graphs.milvus_graph_vector_store import MilvusGraphVectorStore


class FakeCollection:
    def __init__(self):
        self.deleted = []
        self.inserted = []
        self.flush_count = 0

    def delete(self, *, expr):
        self.deleted.append(expr)

    def insert(self, rows):
        self.inserted.append(rows)

    def flush(self):
        self.flush_count += 1


def test_init_connection_selects_graph_database_on_named_alias(monkeypatch):
    calls = []
    monkeypatch.setattr(vector_store_module.connections, "has_connection", lambda alias: False)
    monkeypatch.setattr(
        vector_store_module.connections,
        "connect",
        lambda **kwargs: calls.append(("connect", kwargs)),
    )
    monkeypatch.setattr(
        vector_store_module.db,
        "list_database",
        lambda **kwargs: calls.append(("list", kwargs)) or [],
    )
    monkeypatch.setattr(
        vector_store_module.db,
        "create_database",
        lambda database, **kwargs: calls.append(("create", database, kwargs)),
    )
    monkeypatch.setattr(
        vector_store_module.db,
        "using_database",
        lambda database, **kwargs: calls.append(("using", database, kwargs)),
    )
    store = object.__new__(MilvusGraphVectorStore)
    store.connection_alias = "graph-alias"
    store.milvus_uri = "http://milvus:19530"
    store.milvus_token = ""
    store.milvus_db = "yuxi"

    store._init_connection()

    assert ("list", {"using": "graph-alias"}) in calls
    assert ("create", "yuxi", {"using": "graph-alias"}) in calls
    assert ("using", "yuxi", {"using": "graph-alias"}) in calls


def test_delete_ids_flushes_before_follow_up_visibility_check(monkeypatch):
    collection = FakeCollection()
    monkeypatch.setattr(vector_store_module.utility, "has_collection", lambda *args, **kwargs: True)
    monkeypatch.setattr(vector_store_module, "Collection", lambda *args, **kwargs: collection)
    store = object.__new__(MilvusGraphVectorStore)
    store.connection_alias = "test"

    store._delete_ids("entities", ["a", "b"])

    assert collection.deleted == ['id in ["a", "b"]']
    assert collection.flush_count == 1


def test_graph_record_insert_flushes_before_reconciliation():
    collection = FakeCollection()
    store = object.__new__(MilvusGraphVectorStore)

    store._insert_entities(
        collection,
        [{"entity_id": "a", "content": "alpha"}],
        [[0.1, 0.2]],
    )

    assert collection.inserted
    assert collection.flush_count == 1
