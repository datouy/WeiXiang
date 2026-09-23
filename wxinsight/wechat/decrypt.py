"""数据库解密：把 SQLCipher 加密库整库解密为明文 SQLite。"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

from .. import config as C
from . import cipher


def decrypt_file(
    src: Path,
    dst: Path,
    key_hex: str,
    log: Callable[[str], None] = print,
    verify: bool = True,
    raw: bool = False,
) -> Path:
    """解密单个数据库文件。dst 已是最新则直接返回。

    raw=True 时把 key 当裸 AES-256 密钥（微信 4.1+ 的形态）；
    否则把 key 当 passphrase（PBKDF2 256000 轮派生）。
    """
    src = Path(src)
    dst = Path(dst)

    if not src.exists():
        raise FileNotFoundError(src)

    src_stat = src.stat()
    # 已解密则复用快照（会话内不因源库 mtime 变化而重解密，避免文件锁冲突）
    if dst.exists() and dst.stat().st_size > 0:
        return dst

    key = bytes.fromhex(key_hex)
    with open(src, "rb") as f:
        page1 = f.read(C.PAGE_SIZE)
    if len(page1) < C.PAGE_SIZE:
        raise ValueError(f"{src} 不足一页")
    salt = page1[: C.SALT_SIZE]

    if raw:
        if not cipher.validate_raw_key(page1, key, salt):
            raise RuntimeError(f"密钥校验失败(raw): {src.name}")
        enc_key, mac_key = cipher.derive_raw_keys(key, salt)
    else:
        if not cipher.validate_key(page1, key, salt):
            raise RuntimeError(f"密钥校验失败: {src.name}")
        enc_key, mac_key = cipher.derive_keys(key, salt)
    total_pages = (src_stat.st_size + C.PAGE_SIZE - 1) // C.PAGE_SIZE

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    t0 = time.time()
    zero_page = b"\x00" * C.PAGE_SIZE

    with open(src, "rb") as fin, open(tmp, "wb") as fout:
        fout.write(C.SQLITE_HEADER)
        first = fin.read(C.PAGE_SIZE)
        body = cipher.decrypt_page(first, enc_key, mac_key, 0)
        fout.write(body)  # body 已不含前 16 字节 salt（由 header 顶替）

        page_no = 1
        while True:
            page = fin.read(C.PAGE_SIZE)
            if not page:
                break
            if len(page) < C.PAGE_SIZE:
                page = page + b"\x00" * (C.PAGE_SIZE - len(page))
            if page == zero_page:
                fout.write(zero_page)
            elif verify:
                fout.write(cipher.decrypt_page(page, enc_key, mac_key, page_no))
            else:
                fout.write(_decrypt_page_novfy(page, enc_key, page_no))
            page_no += 1
            if page_no % 20000 == 0:
                log(f"    {src.name}: {page_no}/{total_pages} 页")

    os.replace(tmp, dst)
    dt = time.time() - t0
    mb = src_stat.st_size / 1048576
    log(f"  解密完成 {src.name} → {dst.name}（{mb:.1f}MB / {page_no} 页 / {dt:.1f}s）")
    return dst


def _decrypt_page_novfy(page: bytes, enc_key: bytes, page_no: int) -> bytes:
    from Crypto.Cipher import AES

    offset = C.SALT_SIZE if page_no == 0 else 0
    iv = page[C.PAGE_SIZE - C.RESERVE : C.PAGE_SIZE - C.RESERVE + C.IV_SIZE]
    dec = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(page[offset : C.PAGE_SIZE - C.RESERVE])
    return dec + page[C.PAGE_SIZE - C.RESERVE : C.PAGE_SIZE]


def decrypted_dir(account_dir: Path) -> Path:
    return C.DECRYPT_DIR / account_dir.name


def decrypted_path(account_dir: Path, src: Path) -> Path:
    """把原库路径映射到解密缓存路径（保留相对结构）。"""
    rel = Path(src).resolve().relative_to(Path(account_dir).resolve())
    return decrypted_dir(account_dir) / rel


def ensure_decrypted(
    account_dir: Path,
    src: Path,
    key_hex: str,
    log: Callable[[str], None] = print,
    verify: bool = True,
    raw: bool = False,
) -> Path:
    return decrypt_file(src, decrypted_path(account_dir, src), key_hex, log=log, verify=verify, raw=raw)
