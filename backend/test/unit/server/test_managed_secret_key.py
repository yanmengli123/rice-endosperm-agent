"""Protected runtime must not generate or silently replace the credential master key."""

import pytest

from yuxi.utils import secret_crypto as crypto

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_master_key(monkeypatch):
    crypto._master_key.cache_clear()
    monkeypatch.delenv("YUXI_SECRET_MASTER_KEY", raising=False)
    monkeypatch.delenv("YUXI_SECRET_MASTER_KEY_FILE", raising=False)
    monkeypatch.setenv("YUXI_DB_SKIP_MIGRATIONS", "true")
    yield
    crypto._master_key.cache_clear()


def test_protected_runtime_refuses_development_fallback(monkeypatch, tmp_path):
    path = tmp_path / "must-not-be-generated"
    monkeypatch.setenv("YUXI_DEV_SECRET_KEY_FILE", str(path))
    with pytest.raises(crypto.SecretCryptoError):
        crypto.encrypt_secret("fixture-secret", "fixture")
    assert not path.exists()


def test_existing_ciphertext_survives_preserved_external_key_file(monkeypatch, tmp_path):
    key = "fixture-master-key-" + "a" * 32
    monkeypatch.setenv("YUXI_SECRET_MASTER_KEY", key)
    encrypted = crypto.encrypt_secret("fixture-secret", "fixture")
    crypto._master_key.cache_clear()
    path = tmp_path / "master.key"
    path.write_text(key, encoding="utf-8")
    monkeypatch.delenv("YUXI_SECRET_MASTER_KEY")
    monkeypatch.setenv("YUXI_SECRET_MASTER_KEY_FILE", str(path))
    assert crypto.decrypt_secret(encrypted, "fixture") == "fixture-secret"
    with pytest.raises(crypto.SecretCryptoError):
        crypto.decrypt_secret(encrypted, "wrong-aad")


def test_unreadable_or_conflicting_managed_key_fails_explicitly(monkeypatch, tmp_path):
    monkeypatch.setenv("YUXI_SECRET_MASTER_KEY_FILE", str(tmp_path / "missing"))
    with pytest.raises(crypto.SecretCryptoError, match="无法读取"):
        crypto._master_key()
    monkeypatch.setenv("YUXI_SECRET_MASTER_KEY", "a" * 64)
    with pytest.raises(crypto.SecretCryptoError, match="只能配置一个"):
        crypto._master_key()


def test_operator_pin_is_idempotent_and_never_overwrites_or_prints_key(tmp_path, capsys):
    from scripts.pin_literature_master_key import pin

    source_env, dev_key, target = (tmp_path / name for name in ("source.env", "dev.key", "managed.key"))
    source_env.write_text("", encoding="utf-8")
    dev_key.write_text("fixture-preserved-key-" + "a" * 48, encoding="utf-8")
    pin(source_env=source_env, target=target, dev_key=dev_key)
    pin(source_env=source_env, target=target, dev_key=dev_key)
    assert target.read_bytes() == dev_key.read_bytes()
    original = target.read_bytes()
    dev_key.write_text("fixture-different-key-" + "b" * 48, encoding="utf-8")
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        pin(source_env=source_env, target=target, dev_key=dev_key)
    assert target.read_bytes() == original
    assert "fixture-preserved-key" not in capsys.readouterr().out
