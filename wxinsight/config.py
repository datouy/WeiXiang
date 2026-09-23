"""全局配置与路径。

微信 4.0（Weixin.exe）本地数据库为 SQLCipher 4 加密，参数对齐
chatlog(v0.0.31) internal/wechat/decrypt/windows/v4.go。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

APP_NAME = "wxinsight"
APP_TITLE = "群像谱 · 微信聊天记录分析"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "outputs"
DECRYPT_DIR = DATA_DIR / "decrypted"
CACHE_DIR = DATA_DIR / "cache"
STORE_DB = DATA_DIR / "wxinsight.db"
KEY_CACHE = DATA_DIR / "keys.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
STATIC_DIR = PROJECT_ROOT / "wxinsight" / "web" / "static"

for _d in (DATA_DIR, OUTPUT_DIR, DECRYPT_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------
# 微信 4.0 / SQLCipher4 解密常量
# --------------------------------------------------------------------------
PAGE_SIZE = 4096
KEY_SIZE = 32
SALT_SIZE = 16
IV_SIZE = 16
AES_BLOCK = 16
HMAC_SIZE = 64  # HMAC-SHA512
RESERVE = IV_SIZE + HMAC_SIZE  # 80，天然 16 字节对齐
KDF_ITER = 256000
SQLITE_HEADER = b"SQLite format 3\x00"

# 内存扫描特征串（chatlog: key/windows/v4.go keyPattern）
KEY_PATTERN = bytes(
    [
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x20, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x2F, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    ]
)

WECHAT_V4_PROCESS = "Weixin.exe"
WECHAT_V3_PROCESS = "WeChat.exe"

# 内存扫描约束
SCAN_MIN_REGION = 1024 * 1024  # 跳过 <1MB 的区段
SCAN_CHUNK = 16 * 1024 * 1024  # 单次读取上限
SCAN_ADDR_MIN = 0x10000
SCAN_ADDR_MAX = 0x7FFFFFFFFFFF
PTR_MIN = 0x10000

# --------------------------------------------------------------------------
# 微信数据目录候选（v4: xwechat_files，v3: WeChat Files）
# --------------------------------------------------------------------------
DEFAULT_WECHAT_ROOTS = [
    r"D:\chat\wx\xwechat_files",
    r"C:\Users\%USERNAME%\Documents\xwechat_files",
    r"C:\Users\%USERNAME%\AppData\Roaming\Tencent\xwechat_files",
    r"C:\Users\%USERNAME%\Documents\WeChat Files",
    r"D:\WeChat Files",
    r"E:\WeChat Files",
]

V4_DB_REL = Path("db_storage") / "message" / "message_0.db"
V4_CONTACT_DB = Path("db_storage") / "contact" / "contact.db"
V4_SESSION_DB = Path("db_storage") / "session" / "session.db"


def _expand(p: str) -> str:
    return os.path.expandvars(p)


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_settings(d: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def wechat_roots() -> list[Path]:
    """返回所有候选微信数据根目录（配置优先）。"""
    cfg = load_settings()
    roots: list[Path] = []
    for r in cfg.get("wechat_roots", []) or []:
        roots.append(Path(_expand(r)))
    for r in DEFAULT_WECHAT_ROOTS:
        roots.append(Path(_expand(r)))
    out: list[Path] = []
    seen = set()
    for p in roots:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        if p.exists():
            out.append(p)
    return out


# --------------------------------------------------------------------------
# 关系类型（对齐 jlog-network.html 的调色）
# --------------------------------------------------------------------------
REL_TYPES = {
    "foe": {"label": "宿敌 / 对喷", "color": "#B23A2E"},
    "ally": {"label": "同盟 / 捧哏", "color": "#3C7350"},
    "real": {"label": "现实关系", "color": "#9A6414"},
    "trade": {"label": "交易 / 供应链", "color": "#3A5691"},
    "meme": {"label": "梗 / 被讨论", "color": "#6E5C82"},
}

ROLE_STYLE = {
    "hub": {"label": "引力中心", "color": "#0E6E6C"},
    "key": {"label": "关键人物", "color": "#9A6414"},
    "admin": {"label": "群主", "color": "#0E6E6C"},
    "meme": {"label": "梗的载体", "color": "#6E5C82"},
    "self": {"label": "你自己", "color": "#A8322A"},
    "member": {"label": "成员", "color": "#8C949E"},
}

# 微信消息类型 → 可读标签
MSG_TYPE_LABEL = {
    1: "文本",
    3: "图片",
    34: "语音",
    37: "好友请求",
    42: "名片",
    43: "视频",
    47: "表情",
    48: "位置",
    49: "链接/文件",
    50: "通话",
    51: "通话",
    10000: "系统",
    10002: "系统",
}
