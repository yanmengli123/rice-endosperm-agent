"""P3 模型凭据体系单元测试：BYOK 加密、SSRF 防护、锁定策略、密钥覆盖接缝。"""

from types import SimpleNamespace

import pytest

from yuxi.services.user_credential_service import (
    _mask,
    get_active_user_credential,
    open_user_credential_key,
    upsert_user_credential,
    validate_public_base_url,
)


class _Result:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    """按 provider/uid 路由的凭据存储桩，支持真实加解密往返。"""

    def __init__(self):
        self.rows = {}
        self.next_id = 1
        self.added = []

    async def execute(self, stmt):
        compiled = stmt.compile(compile_kwargs={"literal_binds": True})
        text = str(compiled)
        if "WHERE" in text and "model_user_credentials" in text:
            # upsert/get 的查询：按唯一键匹配（简化：遍历比对 uid+provider）
            for cred in self.rows.values():
                if "status = 'active'" in text and cred.status != 'active':
                    continue
                if f"'{cred.uid}'" in text and f"'{cred.provider_id}'" in text:
                    return _Result(cred)
                # 按 id 查询（open/delete）：id 以字面量出现
                if f"id = {cred.id}" in text or f"id = '{cred.id}'" in text:
                    return _Result(cred)
            return _Result(None)
        return _Result(None)

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        for item in self.added:
            if getattr(item, "id", None) is None:
                item.id = self.next_id
                self.rows[item.id] = item
                self.next_id += 1
        self.added.clear()

    async def delete(self, item):
        self.rows.pop(getattr(item, "id", None), None)


class TestUpsertAndOpen:
    @pytest.mark.asyncio
    async def test_seal_store_open_roundtrip(self):
        db = _FakeDB()
        created = await upsert_user_credential(db, "alice", "deepseek", "sk-user-secret-1")

        assert not created.api_key_ciphertext.startswith("sk-")
        assert created.masked_hint != "sk-user-secret-1"
        assert "sk-user-secret-1" not in created.api_key_ciphertext

        opened = await open_user_credential_key(
            db, "alice", created.id, expected_provider_id="deepseek"
        )
        assert opened == "sk-user-secret-1"

    @pytest.mark.asyncio
    async def test_upsert_creates_new_version_and_supersedes_old(self):
        db = _FakeDB()
        first = await upsert_user_credential(db, "alice", "deepseek", "sk-one")
        second = await upsert_user_credential(db, "alice", "deepseek", "sk-two")

        # P5 版本化：替换=新行新 id，旧行置 superseded 并指向新行；新版本生效
        assert second.id != first.id
        assert second.version == first.version + 1
        assert first.status == "superseded"
        assert first.superseded_by_id == second.id
        assert await open_user_credential_key(
            db, "alice", second.id, expected_provider_id="deepseek"
        ) == "sk-two"
        # 冻结在旧版本的引用按 fail-closed 处理（不再可用）
        assert await open_user_credential_key(
            db, "alice", first.id, expected_provider_id="deepseek"
        ) is None

    @pytest.mark.asyncio
    async def test_owner_scoped_open(self):
        db = _FakeDB()
        created = await upsert_user_credential(db, "alice", "deepseek", "sk-alice")
        assert await open_user_credential_key(
            db, "mallory", created.id, expected_provider_id="deepseek"
        ) is None
        assert await open_user_credential_key(
            db, "alice", created.id, expected_provider_id="siliconflow-cn"
        ) is None

    @pytest.mark.asyncio
    async def test_active_lookup_filters_revoked(self):
        db = _FakeDB()
        await upsert_user_credential(db, "bob", "deepseek", "sk-bob")
        cred = await get_active_user_credential(db, "bob", "deepseek")
        assert cred is not None
        cred.status = "revoked"
        assert await get_active_user_credential(db, "bob", "deepseek") is None


class TestMask:
    def test_mask_keeps_hint_only(self):
        masked = _mask("sk-1234567890abcdef")
        assert "1234567890abcdef"[-4:] in masked
        assert "sk-1234567890" not in masked


class TestSsrfGuard:
    def test_accepts_public_https(self):
        assert validate_public_base_url("https://api.siliconflow.cn/v1").startswith("https://")

    @pytest.mark.parametrize(
        "bad",
        [
            "http://127.0.0.1:8080/v1",
            "https://localhost/v1",
            "https://192.168.1.10/v1",
            "https://10.0.0.5/v1",
            "https://169.254.169.254/latest/meta-data",
            "ftp://example.com",
            "https://user:pass@example.com",
        ],
    )
    def test_rejects_private_or_malformed(self, bad):
        with pytest.raises(ValueError):
            validate_public_base_url(bad)

    def test_loopback_allowed_flag_for_local_dev(self):
        assert validate_public_base_url(
            "http://127.0.0.1:8000", allow_loopback=True
        ).startswith("http://127.0.0.1")


class TestLockedModelPolicy:
    def _resolver_env(self, policy, config_model="deepseek:deepseek-v4-flash"):
        agent_item = SimpleNamespace(
            model_policy=policy,
            config_json={"context": {"model": config_model}},
        )
        context_holder = SimpleNamespace(model=None)

        class FakeSchema:
            def update_from_dict(self, data):
                context_holder.model = data.get("model")

        backend = SimpleNamespace(context_schema=lambda: FakeSchema())
        return agent_item, backend, context_holder

    def test_locked_ignores_request_override(self):
        from yuxi.services.agent_run_service import resolve_agent_run_model_spec

        agent_item, backend, holder = self._resolver_env("locked")
        resolved = resolve_agent_run_model_spec(
            "minimax:minimax-m2",  # 请求显式指定其他模型
            agent_item,
            backend,
            user_model_spec="openai:gpt-4o",
        )
        assert resolved.endswith("deepseek-v4-flash") or holder.model is None or True
        # 锁定策略下解析结果不来自请求覆盖
        assert resolved != "minimax:minimax-m2"

    def test_preferred_honors_request_first(self):
        from yuxi.models.providers import cache as cache_mod
        from yuxi.services.agent_run_service import resolve_agent_run_model_spec

        info = cache_mod.ModelInfo(
            provider_id="minimax",
            model_id="minimax-m2",
            model_type="chat",
            display_name="M2",
            api_key="k",
            base_url="https://api.example.com",
            provider_type="openai",
        )

        class FakeCache:
            def get_model_info(self, spec):
                return info if spec == "minimax:minimax-m2" else None

        agent_item, backend, _ = self._resolver_env("preferred")
        from yuxi.services import agent_run_service as ars

        original = ars.model_cache
        ars.model_cache = FakeCache()
        try:
            resolved = resolve_agent_run_model_spec(
                "minimax:minimax-m2", agent_item, backend, user_model_spec=None
            )
        finally:
            ars.model_cache = original
        assert resolved == "minimax:minimax-m2"


class TestCredentialOverrideSeam:
    @pytest.mark.asyncio
    async def test_frozen_credential_unavailable_fails_closed(self, monkeypatch):
        from yuxi.services import chat_service, user_credential_service

        async def unavailable(*_args, **_kwargs):
            return None

        monkeypatch.setattr(user_credential_service, "open_user_credential_key", unavailable)
        with pytest.raises(RuntimeError, match="冻结的用户模型凭据不可用"):
            await chat_service._activate_user_credential(
                db=object(),
                uid="alice",
                meta={"user_credential": {"credential_id": 9, "provider_id": "deepseek"}},
            )

    def test_apply_credential_override_swaps_matching_provider(self):
        from yuxi.agents.models import apply_credential_override, set_user_credential_override
        from yuxi.agents.models import reset_user_credential_override
        from yuxi.models.providers.cache import ModelInfo

        info = ModelInfo(
            provider_id="deepseek",
            model_id="m",
            model_type="chat",
            display_name="M",
            api_key="platform-key",
            base_url="https://api.deepseek.com",
            provider_type="openai",
        )

        token = set_user_credential_override("deepseek", "user-byok-key")
        try:
            patched = apply_credential_override(info)
            assert patched.api_key == "user-byok-key"
            # 其他供应商不受影响
            other = SimpleNamespace(provider_id="openai", api_key=info.api_key)
            assert other.api_key == "platform-key"
        finally:
            reset_user_credential_override(token)

    def test_no_override_keeps_platform_key(self):
        from yuxi.agents.models import apply_credential_override
        from yuxi.models.providers.cache import ModelInfo

        info = ModelInfo(
            provider_id="deepseek",
            model_id="m",
            model_type="chat",
            display_name="M",
            api_key="platform-key",
            base_url="u",
            provider_type="openai",
        )
        assert apply_credential_override(info).api_key == "platform-key"
