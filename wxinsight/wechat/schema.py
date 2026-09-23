"""微信 4.0 数据库读取层（基于解密后的明文 SQLite）。"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

from .. import config as C
from . import cipher, decrypt

ZSTD_MAGIC = bytes([0x28, 0xB5, 0x2F, 0xFD])

try:
    import zstandard as _zstd

    _ZSTD_DCTX = _zstd.ZstdDecompressor()

    def _zstd_decompress(b: bytes) -> bytes:
        return _ZSTD_DCTX.decompress(b, max_output_size=64 * 1024 * 1024)

except Exception:  # pragma: no cover
    def _zstd_decompress(b: bytes) -> bytes:  # type: ignore
        raise RuntimeError("缺少 zstandard 依赖，无法解压压缩消息")


TALKER_ATTR_RE = re.compile(r"@([^\s@\u2005]{1,32})")
XML_STRIP_RE = re.compile(r"<[^>]+>")
# 微信 XML 里偶发的非法控制字符（会导致 ElementTree 整体解析失败）
XML_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def msg_table(talker: str) -> str:
    return "Msg_" + hashlib.md5(talker.encode("utf-8")).hexdigest()


def _attr(root: ET.Element, name: str, default: str = "") -> str:
    for el in root.iter():
        if el.tag == name and el.text:
            return el.text.strip()
    # 带命名空间的兜底
    for el in root.iter():
        if el.tag.split("}")[-1] == name and el.text:
            return el.text.strip()
    return default


def _parse_xml(content: str) -> dict:
    """解析 type=49 的 appmsg/系统 XML。"""
    try:
        root = ET.fromstring(XML_CTRL_RE.sub("", content or ""))
    except ET.ParseError:
        return {}
    out: dict = {}
    apptype = _attr(root, "type")
    out["app_type"] = apptype
    title = _attr(root, "title")
    des = _attr(root, "des")
    url = _attr(root, "url")
    appname = _attr(root, "appname")
    if title:
        out["title"] = title
    if des:
        out["des"] = des
    if url:
        out["url"] = url

    # 引用回复
    refer = None
    for el in root.iter():
        if el.tag.split("}")[-1] == "refermsg":
            refer = {
                "name": _attr(el, "displayname"),
                "wxid": _attr(el, "chatusr"),
                "content": _attr(el, "content"),
                "type": _attr(el, "type"),
            }
            break
    if refer:
        out["refer"] = refer

    # 文件
    for el in root.iter():
        if el.tag.split("}")[-1] == "appattach":
            fn = _attr(el, "filename") or _attr(el, "attachname")
            size = _attr(el, "totallen")
            if fn:
                out["file"] = {"name": fn, "size": size}
            break

    # 转账 / 红包
    if apptype in ("2000", "2001"):
        out["money"] = _attr(root, "feedesc") or _attr(root, "pay_memo")
    if apptype == "2001":
        out["kind"] = "redpacket"
    if apptype == "2000":
        out["kind"] = "transfer"
    if apptype == "57":
        out["kind"] = "quote"
    if apptype == "5" or appname:
        out.setdefault("kind", "link")
    return out


def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while i < len(b):
        byte = b[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, i
        shift += 7
    raise ValueError("varint 越界")


def parse_chatroom_members(ext_buffer: bytes | None) -> dict[str, str]:
    """解析 chat_room.ext_buffer，返回 {username: 群昵称}。

    结构：字段1(0x0A) = 成员条目；成员内 字段1(0x0A)=username，字段2(0x12)=群昵称。
    """
    out: dict[str, str] = {}
    if not ext_buffer:
        return out
    if isinstance(ext_buffer, str):
        ext_buffer = ext_buffer.encode("utf-8", "replace")
    b = bytes(ext_buffer)
    i = 0
    n = len(b)
    while i < n:
        try:
            tag, i = _read_varint(b, i)
        except ValueError:
            break
        if tag != 0x0A:  # 顶层只关心字段1（成员列表）
            break
        ln, i = _read_varint(b, i)
        member = b[i : i + ln]
        i += ln
        # 成员内部
        username = ""
        display = ""
        j = 0
        m = len(member)
        while j < m:
            try:
                t2, j = _read_varint(member, j)
            except ValueError:
                break
            f2 = t2 >> 3
            wt2 = t2 & 7
            if wt2 == 2:
                ln2, j = _read_varint(member, j)
                val = member[j : j + ln2]
                j += ln2
                if f2 == 1:
                    username = val.decode("utf-8", "replace")
                elif f2 == 2:
                    display = val.decode("utf-8", "replace")
            elif wt2 == 0:
                _, j = _read_varint(member, j)
            elif wt2 == 1:
                j += 8
            elif wt2 == 5:
                j += 4
            else:
                break
        if username:
            out[username] = display or username
    return out


@dataclass
class Message:
    seq: int
    ts: int
    talker: str
    sender_wxid: str
    sender: str  # 显示名
    is_self: bool
    local_type: int
    kind: str
    text: str
    mentions: list[str] = field(default_factory=list)
    quote_name: str = ""
    quote_text: str = ""
    quote_wxid: str = ""
    raw: str = ""

    @property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.ts)


def parse_message_content(content: str, local_type: int, is_chatroom: bool) -> dict:
    """把原始 message_content 解析为结构化字段。"""
    out: dict = {"kind": "other", "text": "", "mentions": [], "quote_name": "", "quote_text": "", "quote_wxid": ""}

    if local_type == 1:
        out["kind"] = "text"
        out["text"] = content
        out["mentions"] = [m for m in TALKER_ATTR_RE.findall(content) if m]
        return out

    if local_type == 3:
        out["kind"] = "image"
        out["text"] = content or "[图片]"
        return out
    if local_type == 34:
        out["kind"] = "voice"
        out["text"] = "[语音]"
        return out
    if local_type == 43:
        out["kind"] = "video"
        out["text"] = "[视频]"
        return out
    if local_type == 47:
        out["kind"] = "sticker"
        out["text"] = "[表情]"
        return out
    if local_type == 48:
        out["kind"] = "location"
        out["text"] = "[位置]"
        return out
    if local_type == 42:
        out["kind"] = "card"
        out["text"] = "[名片]"
        return out
    if local_type == 50:
        out["kind"] = "call"
        out["text"] = "[通话]"
        return out
    if local_type in (10000, 10002):
        out["kind"] = "system"
        out["text"] = XML_STRIP_RE.sub("", content).strip()
        return out

    if local_type == 49:
        info = _parse_xml(content)
        kind = info.get("kind", "link")
        out["kind"] = kind
        out["mentions"] = [m for m in TALKER_ATTR_RE.findall(content) if m][:6]
        if kind == "quote":
            ref = info.get("refer") or {}
            out["quote_name"] = ref.get("name", "")
            out["quote_wxid"] = ref.get("wxid", "")
            qtext = (ref.get("content") or "").strip()
            # 引用的是链接/文件/系统消息时，被引内容本身就是 XML —— 不算有效聊天文本
            if re.search(r"<\s*(?:msg|appmsg)\b|<\?xml", qtext, re.I):
                qtext = ""
            out["quote_text"] = qtext
            out["text"] = (f"[引用 {out['quote_name']}] " + qtext[:120]) if qtext else f"[引用 {out['quote_name']}]"
        elif kind == "redpacket":
            out["text"] = "[红包]"
        elif kind == "transfer":
            out["text"] = "[转账]"
        elif "file" in info:
            out["kind"] = "file"
            out["text"] = f"[文件] {info['file']['name']}"
        else:
            t = info.get("title") or info.get("des") or "[链接]"
            out["text"] = f"[链接] {t}"
        return out

    out["text"] = XML_STRIP_RE.sub("", content).strip()[:200] or f"[类型{local_type}]"
    return out


class WxV4:
    """一个账号的数据访问入口。"""

    def __init__(
        self,
        account_dir: Path,
        keys: dict[str, str],
        log: Callable[[str], None] = print,
        verify: bool = True,
    ) -> None:
        self.account_dir = Path(account_dir)
        self.keys = keys  # {salt_hex: key_hex}，每个库一把裸密钥
        self.log = log
        self.verify = verify
        self._con_cache: dict[str, sqlite3.Connection] = {}

    # -- 解密 --------------------------------------------------------------
    def db(self, rel: Path) -> Path:
        src = self.account_dir / rel
        salt = cipher.salt_of(src).hex()
        if salt not in self.keys:
            raise KeyError(f"缺少 {src.name} 的密钥（salt={salt[:16]}…）")
        return decrypt.ensure_decrypted(
            self.account_dir, src, self.keys[salt], log=self.log, verify=self.verify, raw=True
        )

    def connect(self, path: Path) -> sqlite3.Connection:
        k = str(path)
        if k not in self._con_cache:
            # 只读连接，允许跨线程使用（批量扫描在后台线程跑）
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
            con.row_factory = sqlite3.Row
            self._con_cache[k] = con
        return self._con_cache[k]

    def close(self) -> None:
        for con in self._con_cache.values():
            try:
                con.close()
            except Exception:
                pass
        self._con_cache.clear()

    # -- 基础数据 ----------------------------------------------------------
    def message_db_paths(self) -> list[Path]:
        out = []
        d = self.account_dir / "db_storage" / "message"
        for p in sorted(d.glob("message_*.db")):
            if p.name.endswith(("-shm", "-wal")):
                continue
            out.append(p)
        return out

    def sessions(self) -> list[dict]:
        src = self.account_dir / C.V4_SESSION_DB
        if not src.exists():
            return []
        con = self.connect(self.db(C.V4_SESSION_DB))
        rows = con.execute(
            "SELECT username, summary, last_timestamp, last_msg_sender, "
            "last_sender_display_name FROM SessionTable ORDER BY sort_timestamp DESC"
        )
        return [dict(r) for r in rows]

    def contacts(self) -> dict[str, dict]:
        src = self.account_dir / C.V4_CONTACT_DB
        if not src.exists():
            return {}
        con = self.connect(self.db(C.V4_CONTACT_DB))
        out: dict[str, dict] = {}
        for r in con.execute(
            "SELECT username, local_type, alias, remark, nick_name FROM contact"
        ):
            out[r["username"]] = dict(r)
        return out

    def chatrooms(self) -> dict[str, dict]:
        src = self.account_dir / C.V4_CONTACT_DB
        if not src.exists():
            return {}
        con = self.connect(self.db(C.V4_CONTACT_DB))
        try:
            rows = con.execute("SELECT username, owner, ext_buffer FROM chat_room")
        except sqlite3.Error:
            return {}
        out: dict[str, dict] = {}
        for r in rows:
            out[r["username"]] = {"owner": r["owner"], "ext_buffer": r["ext_buffer"]}
        return out

    def display_name(self, username: str, contacts: dict | None = None) -> str:
        c = (contacts or {}).get(username)
        if c:
            return c.get("remark") or c.get("nick_name") or c.get("alias") or username
        return username

    def room_member_names(self, username: str) -> dict[str, str]:
        """群成员 {username: 群昵称}。"""
        rooms = self.chatrooms()
        r = rooms.get(username)
        if not r:
            return {}
        return parse_chatroom_members(r.get("ext_buffer"))

    # -- 消息 --------------------------------------------------------------
    def iter_messages(
        self,
        talker: str,
        dbs: list[Path] | None = None,
        since: int | None = None,
        until: int | None = None,
    ) -> Iterator[Message]:
        table = msg_table(talker)
        is_room = talker.endswith("@chatroom")
        for dbp in dbs or self.message_db_paths():
            try:
                out = self.db(dbp.relative_to(self.account_dir))
            except FileNotFoundError:
                continue
            con = self.connect(out)
            exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue
            sql = (
                f"SELECT sort_seq, local_type, real_sender_id, create_time, "
                f"message_content, compress_content, status FROM {table} "
                f"WHERE create_time >= ? AND create_time <= ? ORDER BY sort_seq ASC"
            )
            lo = since or 0
            hi = until or 4102444800
            cur = con.execute(sql, (lo, hi))
            n2id = self._load_name2id(con)
            for row in cur:
                yield self._row_to_message(row, talker, is_room, n2id)

    @staticmethod
    def _load_name2id(con: sqlite3.Connection) -> dict[int, str]:
        """加载当前库的 Name2Id: rowid -> wxid，用于 real_sender_id 归因。"""
        out: dict[int, str] = {}
        try:
            for rowid, uname in con.execute("SELECT rowid, user_name FROM Name2Id"):
                out[int(rowid)] = uname
        except sqlite3.Error:
            pass
        return out

    def _row_to_message(
        self, row, talker: str, is_room: bool, n2id: dict[int, str] | None = None
    ) -> Message:
        content = self._decode_content(row["message_content"], row["compress_content"])
        sender = ""
        if is_room:
            head, sep, rest = content.partition(":\n")
            # 前缀必须是单行短头（wxid 或昵称），防止误切正文里带 ":\n" 的消息
            if sep and 0 < len(head.strip()) <= 64 and "\n" not in head:
                sender = head.strip()
                content = rest
        # 微信 4.x 的 local_type 是位压缩的：低 32 位才是真实类型，
        # 高位是 appmsg 子类型（如 57<<32|49 = 引用回复）。不掩码则引用/链接全部丢失。
        lt = int(row["local_type"] or 0)
        if lt > 0xFFFFFFFF:
            lt = lt & 0xFFFFFFFF
        info = parse_message_content(content, lt, is_room)
        ts = int(row["create_time"] or 0)
        try:
            sender_wxid = (n2id or {}).get(int(row["real_sender_id"] or 0), "")
        except (TypeError, ValueError):
            sender_wxid = ""
        return Message(
            seq=int(row["sort_seq"] or 0),
            ts=ts,
            talker=talker,
            sender_wxid=sender_wxid,
            sender=sender,
            is_self=(not is_room and sender == talker) or bool(sender_wxid and sender_wxid == talker),
            local_type=lt,
            kind=info["kind"],
            text=info["text"],
            mentions=info["mentions"],
            quote_name=info["quote_name"],
            quote_text=info["quote_text"],
            quote_wxid=info.get("quote_wxid", ""),
            raw=content[:4000],
        )

    @staticmethod
    def _decode_content(message_content, compress_content) -> str:
        raw: bytes | str | None = None
        if isinstance(message_content, bytes):
            raw = message_content
        elif message_content:
            raw = message_content
        elif isinstance(compress_content, bytes):
            raw = compress_content
        elif compress_content:
            raw = compress_content

        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        if raw.startswith(ZSTD_MAGIC):
            try:
                return _zstd_decompress(raw).decode("utf-8", "replace")
            except Exception:
                return "[解压失败]"
        return raw.decode("utf-8", "replace")

    # -- 会话解析 ----------------------------------------------------------
    def resolve_chat(self, query: str, contacts: dict | None = None) -> list[dict]:
        """按 wxid / 备注名 / 昵称 模糊匹配会话。"""
        q = (query or "").strip().lower()
        contacts = contacts if contacts is not None else self.contacts()
        rooms = self.chatrooms()
        out: list[dict] = []
        for s in self.sessions():
            user = s["username"]
            if user.endswith(("@chatroom",)):
                name = self._room_name(user, contacts, rooms)
                kind = "group"
            elif user == "filehelper":
                name, kind = "文件传输助手", "contact"
            else:
                name = self.display_name(user, contacts)
                kind = "contact"
            s["display"] = name
            s["kind"] = kind
            s["member_count"] = self._room_member_count(user, rooms) if kind == "group" else 0
            if not q or q in user.lower() or q in name.lower():
                out.append(s)
        return out

    def _room_name(self, username: str, contacts: dict, rooms: dict) -> str:
        c = contacts.get(username)
        if c:
            nm = c.get("remark") or c.get("nick_name")
            if nm:
                return nm
        return username

    def _room_member_count(self, username: str, rooms: dict) -> int:
        r = rooms.get(username)
        if not r or not r.get("ext_buffer"):
            return 0
        return _count_members(r["ext_buffer"])


def _count_members(ext_buffer) -> int:
    """ext_buffer 是 protobuf，成员项里含 wxid 字符串；用粗略计数即可。"""
    if ext_buffer is None:
        return 0
    if isinstance(ext_buffer, str):
        data = ext_buffer.encode("utf-8", "replace")
    else:
        data = bytes(ext_buffer)
    # 每个成员条目里都有 wxid / @chatroom 形态的字符串
    return len(re.findall(rb"wxid_[0-9a-zA-Z]{6,}|[0-9a-zA-Z_\-]{6,}@chatroom", data))
