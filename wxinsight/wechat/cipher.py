"""SQLCipher 4 原语：密钥派生、页面校验、页面解密。

严格对齐 chatlog v0.0.31:
  internal/wechat/decrypt/windows/v4.go
  internal/wechat/decrypt/common/common.go
"""

from __future__ import annotations

import hashlib
import hmac
import struct

from Crypto.Cipher import AES

from .. import config as C


def xor_byte(data: bytes, b: int) -> bytes:
    return bytes(x ^ b for x in data)


def pbkdf2(key: bytes, salt: bytes, iterations: int, dklen: int = C.KEY_SIZE) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", key, salt, iterations, dklen)


def derive_keys(key: bytes, salt: bytes) -> tuple[bytes, bytes]:
    """返回 (enc_key, mac_key)。

    enc_key = PBKDF2-HMAC-SHA512(key, salt, 256000, 32)
    mac_key = PBKDF2-HMAC-SHA512(enc_key, salt ^ 0x3a, 2, 32)
    """
    enc_key = pbkdf2(key, salt, C.KDF_ITER)
    mac_salt = xor_byte(salt, 0x3A)
    mac_key = pbkdf2(enc_key, mac_salt, 2)
    return enc_key, mac_key


def page1_hmac(page1: bytes, mac_key: bytes) -> bytes:
    """第一页的 HMAC：page1[SALT:pageSize-64] + LE32(1)。"""
    end = C.PAGE_SIZE - C.RESERVE + C.IV_SIZE  # pageSize - 64
    mac = hmac.new(mac_key, digestmod=hashlib.sha512)
    mac.update(page1[C.SALT_SIZE:end])
    mac.update(struct.pack("<I", 1))  # 页码从 1 开始
    return mac.digest()


def validate_key(page1: bytes, key: bytes, salt: bytes) -> bool:
    """页面 1 的 HMAC 校验 —— 用来判断候选密钥是否命中。"""
    if len(key) != C.KEY_SIZE or len(page1) < C.PAGE_SIZE:
        return False
    _enc_key, mac_key = derive_keys(key, salt)
    calc = page1_hmac(page1, mac_key)
    data_end = C.PAGE_SIZE - C.RESERVE + C.IV_SIZE
    stored = page1[data_end : data_end + C.HMAC_SIZE]
    return hmac.compare_digest(calc, stored)


def raw_mac_key(key: bytes, salt: bytes) -> bytes:
    """裸密钥模式：enc_key 就是候选本身，只派生 mac_key（仅 2 轮 PBKDF2，极快）。"""
    return pbkdf2(key, xor_byte(salt, 0x3A), 2)


def derive_raw_keys(key: bytes, salt: bytes) -> tuple[bytes, bytes]:
    """裸密钥模式：enc_key = key（直接用），mac_key = PBKDF2(key, salt^0x3a, 2)。"""
    return key, raw_mac_key(key, salt)


def validate_raw_key(page1: bytes, key: bytes, salt: bytes) -> bool:
    """把候选当成"直接用"AES-256 裸密钥来校验（微信 4.x 可能的形态）。

    只做 2 轮 PBKDF2，几乎零成本，因此可以海量试探。
    """
    if len(key) != C.KEY_SIZE or len(page1) < C.PAGE_SIZE:
        return False
    data_end = C.PAGE_SIZE - C.RESERVE + C.IV_SIZE
    stored = page1[data_end : data_end + C.HMAC_SIZE]
    return hmac.compare_digest(page1_hmac(page1, raw_mac_key(key, salt)), stored)


def decrypt_page(page: bytes, enc_key: bytes, mac_key: bytes, page_no: int) -> bytes:
    """解密单页（page_no 从 0 开始），返回 pageSize 字节。

    第 0 页前 16 字节是 salt，解密后由调用方写入 SQLite 头。
    """
    offset = C.SALT_SIZE if page_no == 0 else 0
    data_end = C.PAGE_SIZE - C.RESERVE + C.IV_SIZE

    mac = hmac.new(mac_key, digestmod=hashlib.sha512)
    mac.update(page[offset:data_end])
    mac.update(struct.pack("<I", page_no + 1))
    if not hmac.compare_digest(mac.digest(), page[data_end : data_end + C.HMAC_SIZE]):
        raise ValueError(f"第 {page_no} 页 HMAC 校验失败")

    iv = page[C.PAGE_SIZE - C.RESERVE : C.PAGE_SIZE - C.RESERVE + C.IV_SIZE]
    cipher = AES.new(enc_key, AES.MODE_CBC, iv)
    encrypted = page[offset : C.PAGE_SIZE - C.RESERVE]
    decrypted = cipher.decrypt(encrypted)
    return decrypted + page[C.PAGE_SIZE - C.RESERVE : C.PAGE_SIZE]


def read_page1(path) -> bytes:
    with open(path, "rb") as f:
        data = f.read(C.PAGE_SIZE)
    if len(data) < C.PAGE_SIZE:
        raise ValueError(f"{path} 不足一页，无法处理")
    if data[:15] == C.SQLITE_HEADER[:15]:
        raise ValueError(f"{path} 已是明文，无需解密")
    return data


def salt_of(path) -> bytes:
    return read_page1(path)[: C.SALT_SIZE]
