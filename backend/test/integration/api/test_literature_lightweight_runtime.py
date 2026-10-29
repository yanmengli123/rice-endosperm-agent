"""Opt-in live HTTP acceptance using a temporary read-only literature API key.

Run inside the API container after activating its restricted runtime role with
YUXI_LIGHT_HTTP_ACCEPTANCE=1 and --confcutdir=test/integration/api. No provider,
enrichment, ingestion or translation work is submitted. Existing documents are
read; the short-lived test key is disabled in fixture cleanup.
"""

import hashlib
import os
import secrets
from datetime import timedelta
from urllib.parse import quote

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb

from yuxi.utils.datetime_utils import utc_now_naive

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv("YUXI_LIGHT_HTTP_ACCEPTANCE") != "1", reason="Requires explicit live read-only HTTP acceptance"
)]


@pytest.fixture(scope="module")
def runtime():
    url = os.environ["POSTGRES_URL"].replace("+asyncpg", "").replace("+psycopg", "")
    with psycopg.connect(url) as db:
        role = db.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
        assert role == (False, False), "Activate the restricted API database role before HTTP acceptance"
        admin = db.execute(
            "SELECT u.id,u.uid,u.department_id,m.tenant_id FROM users u "
            "JOIN tenant_memberships m ON m.uid=u.uid AND m.status='active' "
            "JOIN tenants t ON t.id=m.tenant_id AND t.status='active' "
            "WHERE u.role IN ('admin','superadmin') AND NOT u.is_disabled AND u.is_deleted=0 "
            "AND u.department_id IS NOT NULL "
            "AND (SELECT count(*) FROM tenant_memberships tm JOIN tenants tt ON tt.id=tm.tenant_id "
            "WHERE tm.uid=u.uid AND tm.status='active' AND tt.status='active')=1 "
            "ORDER BY (u.role='superadmin') DESC,u.id LIMIT 1"
        ).fetchone()
        assert admin is not None, "A valid department-bound administrator with one active tenant is required"
        user_id, uid, department_id, tenant = admin
        db.execute("SELECT set_config('yuxi.tenant_id',%s,true),set_config('yuxi.uid',%s,true)",
                   (str(tenant), uid))
        secret = "yxkey_" + secrets.token_hex(32)
        key_id = db.execute(
            "INSERT INTO api_keys(key_hash,key_prefix,name,user_id,department_id,expires_at,is_enabled,"
            "purpose,scopes,tenant_id,created_by,created_at) VALUES (%s,%s,%s,%s,%s,%s,true,%s,%s,%s,%s,%s) "
            "RETURNING id",
            (hashlib.sha256(secret.encode()).hexdigest(), secret[:12], "literature-light-http-acceptance",
             user_id, department_id, utc_now_naive() + timedelta(minutes=15), "external_agent",
             Jsonb(["literature:read"]), tenant, uid, utc_now_naive()),
        ).fetchone()[0]
        db.commit()
        try:
            with httpx.Client(
                base_url=os.getenv("YUXI_LIGHT_HTTP_BASE_URL", "http://127.0.0.1:5050"),
                headers={"Authorization": f"Bearer {secret}"}, timeout=httpx.Timeout(30, connect=5),
            ) as client:
                yield client, tenant
        finally:
            db.execute("UPDATE api_keys SET is_enabled=false WHERE id=%s", (key_id,))
            db.commit()


def test_catalog_pagination_and_task_projection_are_private(runtime):
    client, _tenant = runtime
    response = client.get("/api/literature/papers", params={"page": 1, "page_size": 2})
    assert response.status_code == 200
    catalog = response.json()
    assert catalog["page"] == 1 and catalog["page_size"] == 2
    assert catalog["total"] >= len(catalog["items"]) > 0, "Existing literature is required for read acceptance"
    assert len(catalog["items"]) <= 2 and all(item.get("rid") for item in catalog["items"])
    assert response.headers.get("cache-control") == "private, no-store"
    second = client.get("/api/literature/papers", params={"page": 2, "page_size": 2})
    assert second.status_code == 200 and second.json()["page"] == 2
    assert not ({item["rid"] for item in catalog["items"]} & {item["rid"] for item in second.json()["items"]})
    tasks = client.get("/api/literature/tasks", params={"limit": 5})
    assert tasks.status_code == 200 and isinstance(tasks.json()["tasks"], list)
    assert len(tasks.json()["tasks"]) <= 5
    assert tasks.headers.get("cache-control") == "private, no-store"
    invalid = client.get("/api/literature/papers", params={"page_size": 101})
    assert invalid.status_code == 422


def test_original_reader_and_source_pdf_are_available(runtime):
    client, _tenant = runtime
    response = client.get("/api/literature/papers", params={"source": "pdf", "page_size": 20})
    assert response.status_code == 200
    candidates = response.json()["items"]
    assert candidates, "An existing parsed PDF is required for original-reader acceptance"
    for item in candidates:
        rid = quote(item["rid"], safe="")
        reader = client.get(f"/api/literature/papers/{rid}/reader")
        assert reader.status_code in (200, 404), "Reader must not fail with a server error under the runtime role"
        if reader.status_code != 200 or not reader.json().get("ready"):
            continue
        assert reader.json()["rid"] == item["rid"] and reader.json()["markdown"].strip()
        assert reader.headers.get("cache-control") == "private, no-store"
        pdf = client.get(f"/api/literature/papers/{rid}/source-pdf")
        assert pdf.status_code == 200, "A ready existing PDF reader must retain its original PDF"
        assert pdf.headers.get("content-type", "").startswith("application/pdf")
        assert pdf.content.startswith(b"%PDF-")
        assert pdf.headers.get("cache-control") == "private, no-store"
        return
    pytest.fail("No existing parsed PDF has a ready original reader; ingestion was not started by this test")


def test_anonymous_and_foreign_asset_requests_are_denied(runtime):
    client, tenant = runtime
    anonymous = client.get("/api/literature/papers", headers={"Authorization": ""})
    assert anonymous.status_code == 401
    foreign = client.get("/api/literature/assets", params={
        "uri": f"kbasset://tenants/{tenant + 1}/documents/http-acceptance/reader_render/render.md"
    })
    assert foreign.status_code == 403
    assert "detail" in foreign.json()


def test_read_scope_cannot_submit_enrichment(runtime):
    client, _tenant = runtime
    response = client.post("/api/literature/enrich/pending", json={"batch_size": 1})
    assert response.status_code == 403
    assert "literature:write" in response.json()["detail"]


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/literature/pdf-translation/settings"),
    ("POST", "/api/literature/pdf-translation/jobs"),
    ("GET", "/api/literature/topics"),
    ("POST", "/api/literature/topics/build"),
    ("GET", "/api/literature/workflows"),
])
def test_retired_workflows_remain_unavailable(runtime, method, path):
    client, _tenant = runtime
    response = client.request(method, path)
    assert response.status_code == 404
