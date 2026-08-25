"""P0 安全收口单元测试：信封加密、缓存密封、管理守卫部门化、账号隔离标识。"""

from types import SimpleNamespace

import pytest

from yuxi.utils.secret_crypto import (
    decrypt_secret,
    encrypt_secret,
    is_encrypted,
    seal_for_cache,
    unseal_from_cache,
)


class TestSecretCrypto:
    def test_roundtrip_with_aad(self):
        sealed = encrypt_secret("sk-test-123", "model-provider-db:p1")
        assert is_encrypted(sealed)
        assert sealed != "sk-test-123"
        assert decrypt_secret(sealed, "model-provider-db:p1") == "sk-test-123"

    def test_aad_mismatch_rejected(self):
        sealed = encrypt_secret("sk-test-123", "model-provider-db:p1")
        with pytest.raises(Exception):
            decrypt_secret(sealed, "model-provider-db:p2")

    def test_encrypt_idempotent_for_ciphertext(self):
        once = encrypt_secret("sk-test", "ctx")
        assert encrypt_secret(once, "ctx") == once

    def test_legacy_plaintext_passthrough_on_decrypt(self):
        assert decrypt_secret("plain-key", "ctx") == "plain-key"
        assert decrypt_secret(None, "ctx") is None

    def test_empty_values(self):
        assert encrypt_secret("", "ctx") == ""
        assert encrypt_secret(None, "ctx") is None

    def test_cache_seal_unseal(self):
        sealed = seal_for_cache("token", "ocr-cache:mineru")
        assert is_encrypted(sealed)
        assert unseal_from_cache(sealed, "ocr-cache:mineru") == "token"
        assert unseal_from_cache("", "ocr-cache:mineru") == ""

    def test_development_key_uses_configured_shared_file(self, tmp_path, monkeypatch):
        from yuxi.utils import secret_crypto

        key_file = tmp_path / "shared" / "master-key"
        monkeypatch.delenv("YUXI_SECRET_MASTER_KEY", raising=False)
        monkeypatch.setenv("YUXI_ENV", "development")
        monkeypatch.setenv("YUXI_DEV_SECRET_KEY_FILE", str(key_file))
        secret_crypto._master_key.cache_clear()
        try:
            sealed = secret_crypto.encrypt_secret("value", "ctx")
            assert key_file.exists()
            assert secret_crypto.decrypt_secret(sealed, "ctx") == "value"
        finally:
            secret_crypto._master_key.cache_clear()


class TestProviderSecretPersistence:
    def _repo(self):
        from yuxi.models.providers.repository import _seal_provider_secrets

        return _seal_provider_secrets

    def test_create_seals_api_key_and_headers(self):
        seal = self._repo()
        data = {"provider_id": "p1", "api_key": "sk-live", "headers_json": {"X-Token": "v1"}}
        sealed = seal("p1", dict(data))
        assert is_encrypted(sealed["api_key"])
        assert is_encrypted(sealed["headers_json"]["X-Token"])
        # 原始入参不被篡改
        assert data["api_key"] == "sk-live"

    def test_masked_header_roundtrip_preserves_existing_ciphertext(self):
        from yuxi.utils.secret_crypto import SECRET_UNCHANGED_MARKER

        sealed = self._repo()(
            "p1",
            {"headers_json": {"X-Token": SECRET_UNCHANGED_MARKER}},
            existing_headers={"X-Token": "enc.v1:existing"},
        )
        assert sealed["headers_json"] == {"X-Token": "enc.v1:existing"}

    def test_resolve_roundtrip(self):
        from yuxi.models.providers.service import resolve_api_key

        seal = self._repo()
        sealed = seal("p2", {"provider_id": "p2", "api_key": "sk-abc"})
        provider = SimpleNamespace(provider_id="p2", api_key=sealed["api_key"])
        assert resolve_api_key(provider) == "sk-abc"

    def test_resolve_passes_through_legacy_plaintext(self):
        from yuxi.models.providers.service import resolve_api_key

        provider = SimpleNamespace(provider_id="p3", api_key="legacy-raw")
        assert resolve_api_key(provider) == "legacy-raw"


class TestModelCacheSealedPayload:
    def test_redis_payload_contains_no_plaintext(self, monkeypatch):
        import json

        from yuxi.models.providers import cache as cache_mod

        stored = {}

        class FakeRedis:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def set(self, key, value):
                stored[key] = value

            def get(self, key):
                return stored.get(key)

            def delete(self, key):
                stored.pop(key, None)

        monkeypatch.setattr(cache_mod, "sync_redis_client", FakeRedis)

        info = cache_mod.ModelInfo(
            provider_id="p",
            model_id="m",
            model_type="chat",
            display_name="M",
            api_key="sk-plain-visible",
            base_url="https://api.example.com",
            provider_type="openai",
            headers={"X-Token": "header-secret"},
        )
        cache_mod.model_cache._save_cache({"p:m": info})

        raw = json.loads(stored[cache_mod.REDIS_CACHE_KEY])
        payload = raw["p:m"]
        assert payload["api_key"] != "sk-plain-visible"
        assert "sk-plain-visible" not in json.dumps(raw)
        assert "header-secret" not in json.dumps(raw)

        loaded = cache_mod.ModelInfo.from_dict(payload)
        assert loaded.api_key == "sk-plain-visible"
        assert loaded.headers == {"X-Token": "header-secret"}


class TestManageGuardsDeptScope:
    def _user(self, role, department_id=None, uid="u-editor"):
        return SimpleNamespace(role=role, department_id=department_id, uid=uid)

    def _agent(self, created_by="system"):
        return SimpleNamespace(created_by=created_by)

    def test_superadmin_manages_anything(self):
        from yuxi.repositories.agent_repository import user_can_manage_agent

        assert user_can_manage_agent(self._user("superadmin"), self._agent("someone"))

    def test_creator_manages_own(self):
        from yuxi.repositories.agent_repository import user_can_manage_agent

        assert user_can_manage_agent(self._user("admin", 1, uid="u-a"), self._agent("u-a"))

    def test_cross_dept_admin_denied_without_creator_dept(self):
        from yuxi.repositories.agent_repository import user_can_manage_agent

        assert not user_can_manage_agent(
            self._user("admin", 2, uid="u-b"), self._agent("u-other")
        )

    def test_same_dept_admin_allowed(self):
        from yuxi.repositories.agent_repository import user_can_manage_agent

        assert user_can_manage_agent(
            self._user("admin", 5, uid="u-b"), self._agent("u-other"), creator_department_id=5
        )

    def test_other_dept_admin_denied(self):
        from yuxi.repositories.agent_repository import user_can_manage_agent

        assert not user_can_manage_agent(
            self._user("admin", 6, uid="u-b"), self._agent("u-other"), creator_department_id=5
        )

    def test_system_resources_only_superadmin(self):
        from yuxi.repositories.agent_repository import user_can_manage_agent

        assert not user_can_manage_agent(
            self._user("admin", 5, uid="u-x"), self._agent("system"), creator_department_id=5
        )


class TestAccountScopeDerivation:
    def test_validator_derives_scope_from_uid(self):
        from yuxi.storage.postgres.models_business import User

        user = User(username="n", password_hash="x", uid="tester01")
        assert user.account_scope_id
        assert user.account_scope_id.startswith("yxacct_")

    def test_scope_stable_and_unique_per_uid(self):
        from yuxi.storage.postgres.models_business import User
        from yuxi.utils.auth_utils import AuthUtils

        u1 = User(username="a", password_hash="x", uid="alice")
        u2 = User(username="b", password_hash="x", uid="bob")
        assert u1.account_scope_id == AuthUtils.account_scope_id("alice")
        assert u1.account_scope_id != u2.account_scope_id

    def test_device_key_ttl_constant(self):
        from yuxi.services.auth_service import DEVICE_API_KEY_TTL_DAYS

        assert 30 <= DEVICE_API_KEY_TTL_DAYS <= 180
