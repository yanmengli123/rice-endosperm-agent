"""渠道验签与加解密单测：微信系/飞书往返向量与签名校验。"""

from __future__ import annotations

import base64
import json
import os

import pytest

from yuxi.channels.signing import (
    ChannelSignatureError,
    WeChatCrypto,
    dingtalk_robot_signature,
    feishu_decrypt,
    feishu_signature,
    parse_wechat_xml,
    wechat_signature,
)

pytestmark = [pytest.mark.unit]


def _aes_key_43() -> str:
    """43 位 Base64 EncodingAESKey（解出 32 字节 AES-256 密钥）。"""
    return base64.b64encode(os.urandom(32)).decode().rstrip("=")


class TestWeChatCrypto:
    def test_roundtrip(self) -> None:
        key = _aes_key_43()
        crypto = WeChatCrypto(key, "corp-123")
        xml = "<xml><Content>你好，世界</Content><MsgId>42</MsgId></xml>"
        encrypted = crypto.encrypt(xml)
        assert crypto.decrypt(encrypted) == xml

    def test_receive_id_mismatch_rejected(self) -> None:
        key = _aes_key_43()
        sender = WeChatCrypto(key, "corp-A")
        receiver = WeChatCrypto(key, "corp-B")
        with pytest.raises(ChannelSignatureError):
            receiver.decrypt(sender.encrypt("<xml><Content>x</Content></xml>"))

    def test_invalid_aes_key_rejected(self) -> None:
        with pytest.raises(ChannelSignatureError):
            WeChatCrypto("too-short", "corp")

    def test_tampered_ciphertext_rejected(self) -> None:
        key = _aes_key_43()
        crypto = WeChatCrypto(key, "corp")
        encrypted = crypto.encrypt("<xml><Content>x</Content></xml>")
        raw = bytearray(base64.b64decode(encrypted))
        raw[-1] ^= 0xFF
        with pytest.raises(ChannelSignatureError):
            crypto.decrypt(base64.b64encode(bytes(raw)).decode())

    def test_signature_sorted_sha1(self) -> None:
        signature = wechat_signature("token", "13945316929", "nonce", "encrypt")
        # sha1 输出 40 位十六进制；字典序排序保证 token/nonce 位置无关
        assert len(signature) == 40
        assert signature == wechat_signature("nonce", "13945316929", "token", "encrypt")


class TestFeishuCrypto:
    def test_roundtrip(self) -> None:
        encrypt_key = "test-encrypt-key"
        event = {"schema": "2.0", "header": {"event_id": "e1"}}
        plain = json.dumps(event)
        # 飞书加密：key=sha256(encrypt_key)，IV 取密文前 16 字节，明文即 JSON
        import hashlib

        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        key = hashlib.sha256(encrypt_key.encode()).digest()
        iv = os.urandom(16)
        pad = 16 - (len(plain.encode()) % 16)
        padded = plain.encode() + bytes([pad]) * pad
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        encrypt_field = base64.b64encode(iv + encryptor.update(padded) + encryptor.finalize()).decode()

        assert json.loads(feishu_decrypt(encrypt_key, encrypt_field)) == event

    def test_signature(self) -> None:
        signature = feishu_signature("key", "1609425990", "abc", "body")
        assert len(signature) == 64

    def test_bad_padding_rejected(self) -> None:
        with pytest.raises(ChannelSignatureError):
            feishu_decrypt("key", base64.b64encode(b"x" * 48).decode())


class TestDingTalk:
    def test_robot_signature_deterministic(self) -> None:
        first = dingtalk_robot_signature("secret", "1609425990")
        second = dingtalk_robot_signature("secret", "1609425990")
        assert first == second
        # HMAC-SHA256 的 base64：44 位且以 = 结尾
        assert len(first) == 44 and first.endswith("=")


class TestXml:
    def test_parse_flat_xml(self) -> None:
        parsed = parse_wechat_xml("<xml><ToUserName><![CDATA[wx]]></ToUserName><MsgId>1</MsgId></xml>")
        assert parsed == {"ToUserName": "wx", "MsgId": "1"}

    def test_invalid_xml_rejected(self) -> None:
        with pytest.raises(ChannelSignatureError):
            parse_wechat_xml("not-xml")
