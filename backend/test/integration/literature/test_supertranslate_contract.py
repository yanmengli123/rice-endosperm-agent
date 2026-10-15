"""SuperTranslate bridge 契约与源锁定（集成层，活体端点）。

构建期（Dockerfile 两道 RUN）与 bridge 启动期（lifespan fail-fast）已挡住
契约断裂；本测试对**运行中的服务**再做一次可观测验证，供 supertranslate
profile 启用时锁定行为。未配置 SUPER_TRANSLATE_URL/TOKEN 时跳过（不算失败）。
"""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("SUPER_TRANSLATE_URL") or not os.environ.get("SUPER_TRANSLATE_TOKEN"),
        reason="SUPER_TRANSLATE_URL/TOKEN 未配置（supertranslate 服务未启用）",
    ),
]

BASE = os.environ.get("SUPER_TRANSLATE_URL", "").rstrip("/")
HEADERS = {"Authorization": f"Bearer {os.environ.get('SUPER_TRANSLATE_TOKEN', '')}"}

#: engine.py 依赖的 8 个上游符号（契约报告必须逐个点名）
REQUIRED_SYMBOLS = (
    ("app.services.layout_fix", "fix_translated_layout"),
    ("pdf_zh_translator.ocr", "is_scanned_pdf"),
    ("pdf_zh_translator.ocr", "ocr_pdf_to_searchable"),
    ("pdf_zh_translator.pdf_layout", "translate_pdf"),
    ("pdf_zh_translator.pdf_layout", "verify_translation_issues"),
    ("pdf_zh_translator.pdf_layout", "create_dual_pdf"),
    ("pdf_zh_translator.translators", "VendorTranslator"),
    ("pdf_zh_translator.translators", "CachedTranslator"),
)


def _get(path: str) -> dict:
    response = httpx.get(f"{BASE}{path}", headers=HEADERS, timeout=10)
    assert response.status_code == 200, response.text
    return response.json()


def test_contract_endpoint_all_green():
    body = _get("/contract")
    assert body["contract_ok"] is True
    assert [item for item in body["checks"] if not item["ok"]] == []
    names = {item["name"] for item in body["checks"]}
    for module, attr in REQUIRED_SYMBOLS:
        assert f"import {module}.{attr}" in names
    # 领域术语表在契约面内（rice.json 已 COPY 进 corpora/ 且结构合法）
    assert any("rice.json" in name for name in names)


def test_health_reports_fingerprints():
    body = httpx.get(f"{BASE}/health", timeout=10).json()
    for key in ("status", "engine_revision", "corpus_digest", "font_digest", "contract_ok"):
        assert body.get(key), f"/health 缺少 {key}"
    assert body["status"] == "ok" and body["contract_ok"] is True


def test_source_lock_digest_matches_running_image():
    lock = _get("/contract")["source_lock"]
    assert lock.get("source_digest"), "源摘要未暴露（SOURCE.fingerprint/SOURCE.lock 均缺失）"
    if os.environ.get("SUPER_TRANSLATE_ALLOW_UNPINNED") == "1":
        return  # 开发期显式绕过：只要求摘要可见，不要求与锁一致
    assert lock.get("pinned") is True, f"运行镜像源摘要与锁不一致: {lock}"
