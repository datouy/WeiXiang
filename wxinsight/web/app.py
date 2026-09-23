"""本地 Web 应用：选一个群/好友 → 生成群像谱报告。"""

from __future__ import annotations

import re
import sys
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from wxinsight import config as C  # noqa: E402
from wxinsight.analysis import analyze, render  # noqa: E402
from wxinsight.wechat import extract_keys, locate, schema  # noqa: E402

app = FastAPI(title="群像谱 · 微信聊天记录分析")

_wx = None
_keys = None
_lock = threading.Lock()


def _pick_account():
    accs = [a for a in locate.scan_accounts() if a.version == 4]
    if not accs:
        return None
    # 优先有密钥缓存的账号
    keys = extract_keys.load_keys()
    for a in accs:
        for p in a.message_dbs():
            try:
                salt = __import__("wxinsight.wechat.cipher", fromlist=["cipher"]).salt_of(p).hex()
            except Exception:
                continue
            if salt in keys:
                return a
    return accs[0]


def get_wx():
    global _wx, _keys
    with _lock:
        if _wx is not None:
            return _wx
        acc = _pick_account()
        if acc is None:
            raise HTTPException(500, "未找到微信账号数据目录")
        keys = extract_keys.ensure_keys(acc.data_dir, log=print)
        if not keys:
            raise HTTPException(500, "未获取到数据库密钥，请确认微信已登录")
        _wx = schema.WxV4(acc.data_dir, keys, log=print)
        _keys = keys
        return _wx


def _slug(name: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]", "_", name).strip()
    return s[:60] or "report"


def _chat_label(username: str, display: str) -> tuple[str, str]:
    if username.endswith("@chatroom"):
        return "group", display or username
    if username == "filehelper":
        return "contact", "文件传输助手"
    if username.startswith("gh_"):
        return "official", display or username
    if "brandsessionholder" in username:
        return "official", display or username
    return "contact", display or username


@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(C.STATIC_DIR / "index.html")


@app.get("/api/chats")
def list_chats():
    wx = get_wx()
    rooms = wx.chatrooms()
    contacts = wx.contacts()
    out = []
    for s in wx.sessions():
        u = s["username"]
        kind, label = _chat_label(u, wx.display_name(u, contacts))
        mc = len(rooms.get(u, {}).get("ext_buffer") or b"") and _room_count(rooms, u)
        out.append({
            "username": u,
            "name": label or u,
            "kind": kind,
            "member_count": mc,
            "last_ts": s.get("last_timestamp"),
        })
    out.sort(key=lambda x: ({"group": 0, "contact": 1, "official": 2}.get(x["kind"], 3), -(x["last_ts"] or 0)))
    return JSONResponse(out)


def _room_count(rooms, u):
    r = rooms.get(u)
    if not r:
        return 0
    from wxinsight.wechat.schema import parse_chatroom_members
    return len(parse_chatroom_members(r.get("ext_buffer")))


class AnalyzeReq(BaseModel):
    talker: str


@app.post("/api/analyze")
def do_analyze(req: AnalyzeReq):
    wx = get_wx()
    talker = req.talker
    if not talker:
        raise HTTPException(400, "缺少 talker")

    # 解析显示名
    contacts = wx.contacts()
    label = wx.display_name(talker, contacts)

    log = lambda m: print("  ", m)
    rep = analyze.analyze(wx, talker, log=log)
    fname = _slug(label) + "_群像谱.html"
    out = render.write_report(rep, C.OUTPUT_DIR / fname)
    return {"url": f"/report/{out.name}", "name": label, "meta": rep["meta"]}


@app.get("/report/{fname}")
def report(fname: str):
    p = (C.OUTPUT_DIR / fname).resolve()
    if not p.exists() or p.parent.resolve() != C.OUTPUT_DIR.resolve():
        raise HTTPException(404, "报告不存在")
    return FileResponse(p, media_type="text/html")


@app.get("/api/health")
def health():
    return {"ok": True}
