"""渠道平台验签与报文加解密（纯函数，无 IO）。

覆盖三家密码学体系：
- 微信系（企业微信/公众号）：sha1 四元组签名 + AES-256-CBC（EncodingAESKey 派生）；
- 飞书：sha256 签名 + AES-256-CBC（sha256(encrypt_key) 派生，IV 取密文头部）；
- Telegram：webhook secret_token 常量时间比对。

所有函数失败显式抛 :class:`ChannelSignatureError`，调用方拒绝回调（403）并计数。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class ChannelSignatureError(Exception):
    """验签或解密失败：回调来源不可信或密钥配置不一致。"""


def constant_time_equals(expected: str, actual: str) -> bool:
    return hmac.compare_digest(expected.encode(), actual.encode())


# ---------------------------------------------------------------------------
# 微信系（企业微信 / 公众号共用）
# ---------------------------------------------------------------------------


def wechat_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """微信系回调签名：sha1(字典序排序后的 token/timestamp/nonce/encrypt)。"""
    items = sorted([token or "", timestamp or "", nonce or "", encrypt or ""])
    return hashlib.sha1("".join(items).encode()).hexdigest()


def _pkcs7_unpad(data: bytes, block: int = 32) -> bytes:
    if not data or len(data) % block != 0:
        raise ChannelSignatureError("密文长度非法")
    pad = data[-1]
    if pad < 1 or pad > block or pad > len(data) or data[-pad:] != bytes([pad]) * pad:
        raise ChannelSignatureError("PKCS7 填充非法")
    return data[:-pad]


def _pkcs7_pad(data: bytes, block: int = 32) -> bytes:
    pad = block - (len(data) % block)
    return data + bytes([pad]) * pad


class WeChatCrypto:
    """企业微信/公众号消息加解密（AES-256-CBC，IV 取 AESKey 前 16 字节）。

    明文结构：random(16B) + msg_len(4B 网络序) + msg + receiveid；
    receiveid 校验防跨应用串扰（corp_id / appid）。
    """

    def __init__(self, encoding_aes_key: str, receive_id: str) -> None:
        try:
            self.aes_key = base64.b64decode((encoding_aes_key or "") + "=")
        except Exception as exc:
            raise ChannelSignatureError("EncodingAESKey 非法") from exc
        if len(self.aes_key) != 32:
            raise ChannelSignatureError("EncodingAESKey 必须为 43 位 Base64")
        self.receive_id = receive_id or ""
        self.iv = self.aes_key[:16]

    def decrypt(self, encrypt_b64: str) -> str:
        try:
            ciphertext = base64.b64decode(encrypt_b64)
        except Exception as exc:
            raise ChannelSignatureError("密文 Base64 非法") from exc
        if len(ciphertext) < 48 or len(ciphertext) % 16 != 0:
            raise ChannelSignatureError("密文长度非法")
        decryptor = Cipher(algorithms.AES(self.aes_key), modes.CBC(self.iv)).decryptor()
        plain = _pkcs7_unpad(decryptor.update(ciphertext) + decryptor.finalize())
        if len(plain) < 20:
            raise ChannelSignatureError("明文结构非法")
        msg_len = struct.unpack(">I", plain[16:20])[0]
        msg = plain[20 : 20 + msg_len]
        receive_id = plain[20 + msg_len :].decode()
        if len(msg) != msg_len or (self.receive_id and receive_id != self.receive_id):
            raise ChannelSignatureError("receiveid 校验失败，密钥与应用不匹配")
        return msg.decode()

    def encrypt(self, plain_xml: str) -> str:
        import os

        raw = (
            os.urandom(16) + struct.pack(">I", len(plain_xml.encode())) + plain_xml.encode() + self.receive_id.encode()
        )
        encryptor = Cipher(algorithms.AES(self.aes_key), modes.CBC(self.iv)).encryptor()
        return base64.b64encode(encryptor.update(_pkcs7_pad(raw)) + encryptor.finalize()).decode()


def parse_wechat_xml(xml_text: str) -> dict[str, str]:
    """微信系回调 XML → 扁平 dict（顶层文本节点）。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ChannelSignatureError("回调 XML 解析失败") from exc
    return {child.tag: (child.text or "").strip() for child in root}


# ---------------------------------------------------------------------------
# 飞书
# ---------------------------------------------------------------------------


def feishu_signature(encrypt_key: str, timestamp: str, nonce: str, body: str) -> str:
    """飞书事件签名：sha256(timestamp + nonce + encrypt_key + body)。"""
    material = f"{timestamp}{nonce}{encrypt_key}{body}".encode()
    return hashlib.sha256(material).hexdigest()


def feishu_decrypt(encrypt_key: str, encrypt_b64: str) -> str:
    """飞书 AES-256-CBC 解密：key = sha256(encrypt_key)，IV 取密文前 16 字节。

    解密去填充后整个明文即 JSON 字符串（无长度头），与微信系结构不同。
    """
    try:
        ciphertext = base64.b64decode(encrypt_b64)
    except Exception as exc:
        raise ChannelSignatureError("飞书密文 Base64 非法") from exc
    if len(ciphertext) < 32 or len(ciphertext) % 16 != 0:
        raise ChannelSignatureError("飞书密文长度非法")
    key = hashlib.sha256((encrypt_key or "").encode()).digest()
    decryptor = Cipher(algorithms.AES(key), modes.CBC(ciphertext[:16])).decryptor()
    plain = decryptor.update(ciphertext[16:]) + decryptor.finalize()
    pad = plain[-1]
    if pad < 1 or pad > 16 or plain[-pad:] != bytes([pad]) * pad:
        raise ChannelSignatureError("飞书 PKCS7 填充非法")
    return plain[:-pad].decode()


# ---------------------------------------------------------------------------
# 钉钉（自定义机器人签名 / Stream 模式之外的传统回调）
# ---------------------------------------------------------------------------


def dingtalk_robot_signature(app_secret: str, timestamp: str) -> str:
    """钉钉机器人签名：base64(hmac_sha256(timestamp + \"\\n\" + secret, secret))。"""
    hmac_value = hmac.new(
        app_secret.encode(),
        f"{timestamp}\n{app_secret}".encode(),
        digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(hmac_value).decode()
