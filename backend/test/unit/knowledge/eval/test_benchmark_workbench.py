"""基准逐条构建（authoring）服务层单测：draft 状态机、条目 CRUD、审核、导入、完成锁定、派生版本。"""

from types import SimpleNamespace

import pytest

from yuxi.knowledge.eval.service import (
    DATASET_STATUS_COMPLETED,
    DATASET_STATUS_DRAFT,
    DatasetStateError,
    EvaluationService,
    FinalizeValidationError,
    ItemValidationError,
)


class FakeItem:
    def __init__(self, record):
        self.item_id = record["item_id"]
        self.dataset_id = record["dataset_id"]
        self.kb_id = record.get("kb_id", "db_1")
        self.item_index = record["item_index"]
        self.query_text = record["query_text"]
        self.gold_chunk_ids = record.get("gold_chunk_ids") or []
        self.gold_answer = record.get("gold_answer")
        self.external_id = record.get("external_id")
        self.item_metadata = record.get("item_metadata")
        self.status = record.get("status", "draft")
        self.created_by = record.get("created_by")
        self.created_at = None
        self.updated_at = None


class FakeEvaluationRepository:
    def __init__(self, dataset=None):
        self.dataset = dataset
        self.items: list[FakeItem] = []

    async def get_dataset(self, dataset_id):
        return self.dataset if self.dataset and self.dataset.dataset_id == dataset_id else None

    async def create_dataset(self, payload):
        self.dataset = SimpleNamespace(
            dataset_id=payload["dataset_id"],
            kb_id=payload["kb_id"],
            name=payload["name"],
            description=payload.get("description"),
            item_count=payload.get("item_count", 0),
            has_gold_chunks=payload.get("has_gold_chunks", False),
            has_gold_answers=payload.get("has_gold_answers", False),
            build_metadata=payload.get("build_metadata") or {},
            created_by=payload.get("created_by"),
            created_at=None,
            updated_at=None,
        )
        return self.dataset

    async def create_dataset_with_items(self, payload, records):
        await self.create_dataset(payload)
        for record in records:
            record["dataset_id"] = payload["dataset_id"]
            self.items.append(FakeItem(record))
        return self.dataset

    async def update_dataset(self, dataset_id, data):
        if self.dataset is None or self.dataset.dataset_id != dataset_id:
            return None
        for key, value in data.items():
            setattr(self.dataset, key, value)
        return self.dataset

    async def list_all_dataset_items(self, dataset_id):
        return sorted(
            (item for item in self.items if item.dataset_id == dataset_id), key=lambda item: item.item_index
        )

    async def list_dataset_items(self, dataset_id, offset=0, limit=100, *, status=None, keyword=None):
        items = await self.list_all_dataset_items(dataset_id)
        if status:
            items = [item for item in items if item.status == status]
        if keyword:
            items = [item for item in items if keyword in item.query_text]
        return items[offset : offset + limit]

    async def count_dataset_items(self, dataset_id, *, status=None, keyword=None):
        return len(await self.list_dataset_items(dataset_id, status=status, keyword=keyword))

    async def add_dataset_items(self, records):
        self.items.extend(FakeItem(record) for record in records)

    async def get_dataset_item(self, item_id):
        return next((item for item in self.items if item.item_id == item_id), None)

    async def update_dataset_item(self, item_id, data):
        item = await self.get_dataset_item(item_id)
        if item is None:
            return None
        for key, value in data.items():
            setattr(item, key, value)
        return item

    async def update_dataset_items(self, updates):
        count = 0
        for item_id, data in updates:
            if await self.update_dataset_item(item_id, data):
                count += 1
        return count

    async def delete_dataset_item(self, item_id):
        item = await self.get_dataset_item(item_id)
        if item is None:
            return False
        self.items.remove(item)
        return True

    async def get_max_item_index(self, dataset_id):
        indexes = [item.item_index for item in self.items if item.dataset_id == dataset_id]
        return max(indexes) if indexes else -1

    async def list_external_ids(self, dataset_id):
        return [item.external_id for item in self.items if item.dataset_id == dataset_id and item.external_id]


def _draft_dataset(dataset_id="ds_1", kb_id="db_1", review_required=True):
    return SimpleNamespace(
        dataset_id=dataset_id,
        kb_id=kb_id,
        name="手工基准",
        description="",
        item_count=0,
        has_gold_chunks=False,
        has_gold_answers=False,
        build_metadata={
            "source": "manual",
            "status": DATASET_STATUS_DRAFT,
            "version": 1,
            "review_required": review_required,
        },
        created_by="u1",
        created_at=None,
        updated_at=None,
    )


def _service(repo):
    service = EvaluationService()
    service.eval_repo = repo
    return service


async def test_create_manual_dataset_starts_as_draft():
    repo = FakeEvaluationRepository()
    result = await _service(repo).create_manual_dataset(
        kb_id="db_1", name="回归集", review_required=True, created_by="u1"
    )
    assert result["status"] == DATASET_STATUS_DRAFT
    assert repo.dataset.build_metadata["review_required"] is True
    assert result["item_count"] == 0


async def test_add_item_auto_external_id_and_duplicate_query_rejected():
    repo = FakeEvaluationRepository(_draft_dataset())
    service = _service(repo)

    created = await service.add_dataset_item(
        "ds_1", {"query": "报销时限？", "gold_answer": "30日内。", "answer_type": "fact"}, operator="u1"
    )
    assert created["external_id"] == "item-0001"
    assert created["status"] == "draft"

    with pytest.raises(ItemValidationError) as exc_info:
        await service.add_dataset_item("ds_1", {"query": "报销时限!", "gold_answer": "x"}, operator="u1")
    assert "重复" in exc_info.value.fields["query"]

    with pytest.raises(ItemValidationError) as exc_info:
        await service.add_dataset_item("ds_1", {"query": "新问题", "external_id": "item-0001"}, operator="u1")
    assert exc_info.value.fields["external_id"] == "业务编号已存在"


async def test_update_item_resets_status_only_on_content_change():
    repo = FakeEvaluationRepository(_draft_dataset())
    service = _service(repo)
    created = await service.add_dataset_item(
        "ds_1", {"query": "问题A", "gold_answer": "答案A"}, operator="u1"
    )
    await service.review_dataset_items("ds_1", action="approve", operator="u1")

    unchanged = await service.update_dataset_item(
        "ds_1", created["item_id"], {"query": "问题A", "gold_answer": "答案A"}, operator="u1"
    )
    assert unchanged["status"] == "approved"

    changed = await service.update_dataset_item(
        "ds_1", created["item_id"], {"query": "问题A", "gold_answer": "新答案"}, operator="u1"
    )
    assert changed["status"] == "draft"
    assert changed["item_metadata"]["review_history"]  # 审核历史被保留


async def test_review_requires_reason_for_reject_and_records_history():
    repo = FakeEvaluationRepository(_draft_dataset())
    service = _service(repo)
    await service.add_dataset_item("ds_1", {"query": "q", "gold_answer": "a"}, operator="u1")

    with pytest.raises(ValueError, match="打回必须填写原因"):
        await service.review_dataset_items("ds_1", action="reject", reason=" ", operator="u1")

    result = await service.review_dataset_items("ds_1", action="reject", reason="答案不完整", operator="reviewer")
    assert result["updated"] == 1
    item = repo.items[0]
    assert item.status == "rejected"
    assert item.item_metadata["reject_reason"] == "答案不完整"
    assert item.item_metadata["review_history"][-1]["by"] == "reviewer"


async def test_finalize_enforces_review_then_locks_dataset():
    repo = FakeEvaluationRepository(_draft_dataset())
    service = _service(repo)
    await service.add_dataset_item("ds_1", {"query": "q", "gold_answer": "a"}, operator="u1")

    with pytest.raises(FinalizeValidationError) as exc_info:
        await service.finalize_dataset("ds_1", operator="u1")
    assert exc_info.value.report["errors"][0]["code"] == "review_pending"

    await service.review_dataset_items("ds_1", action="approve", operator="u1")
    result = await service.finalize_dataset("ds_1", operator="u1")
    assert result["dataset"]["status"] == DATASET_STATUS_COMPLETED
    assert repo.dataset.item_count == 1
    assert repo.dataset.has_gold_answers is True

    with pytest.raises(DatasetStateError):
        await service.add_dataset_item("ds_1", {"query": "locked", "gold_answer": "x"}, operator="u1")


async def test_finalize_rejects_mixed_gold_answer_coverage():
    repo = FakeEvaluationRepository(_draft_dataset(review_required=False))
    service = _service(repo)
    await service.add_dataset_item("ds_1", {"query": "q1", "gold_answer": "a1"}, operator="u1")
    await service.add_dataset_item("ds_1", {"query": "q2"}, operator="u1")
    with pytest.raises(FinalizeValidationError) as exc_info:
        await service.finalize_dataset("ds_1", operator="u1")
    assert exc_info.value.report["errors"][0]["code"] == "gold_answer_partial"


async def test_import_items_partial_success_with_line_errors():
    import json

    repo = FakeEvaluationRepository(_draft_dataset())
    service = _service(repo)
    content = "\n".join(
        [
            json.dumps({"query": "问题1", "gold_answer": "答案1"}, ensure_ascii=False),
            "{bad json",
            json.dumps({"query": "问题1"}, ensure_ascii=False),  # 与第一行重复
        ]
    )
    result = await service.import_dataset_items("ds_1", content, operator="u1")
    assert result["added"] == 1
    assert result["rejected"] == 2
    assert [error["line"] for error in result["errors"]] == [2, 3]
    assert repo.items[0].external_id == "item-0001"


async def test_create_dataset_version_copies_items_with_lineage():
    repo = FakeEvaluationRepository(_draft_dataset())
    service = _service(repo)
    await service.add_dataset_item("ds_1", {"query": "q", "gold_answer": "a", "id": "exp-0001"}, operator="u1")
    await service.review_dataset_items("ds_1", action="approve", operator="u1")
    await service.finalize_dataset("ds_1", operator="u1")

    version = await service.create_dataset_version("ds_1", operator="u2")
    assert version["status"] == DATASET_STATUS_DRAFT
    assert version["build_metadata"]["parent_dataset_id"] == "ds_1"
    assert version["build_metadata"]["version"] == 2
    copied = repo.items[-1]
    assert copied.external_id == "exp-0001"
    assert copied.status == "approved"
    assert copied.item_index == repo.items[0].item_index
    assert repo.dataset.dataset_id != "ds_1"  # 父版本保持不可变
