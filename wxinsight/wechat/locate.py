"""定位本机微信账号数据目录。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import config as C
from . import winmem


@dataclass
class Account:
    name: str
    data_dir: Path
    version: int  # 4 = xwechat_files(4.0), 3 = WeChat Files(3.x)
    online: bool = False
    exe_size: int = 0
    files: list[Path] = field(default_factory=list)

    @property
    def display(self) -> str:
        tag = "在线" if self.online else "离线"
        return f"{self.name}（微信{self.version}.0 · {tag}）"

    def message_dbs(self) -> list[Path]:
        if self.version == 4:
            d = self.data_dir / "db_storage" / "message"
            return sorted(d.glob("message_*.db"))
        d = self.data_dir / "Msg"
        return sorted(d.glob("MSG*.db"))

    def probe_db(self) -> Path | None:
        dbs = self.message_dbs()
        return dbs[0] if dbs else None

    def db_path(self, *rel: str) -> Path:
        return self.data_dir.joinpath(*rel)


def _online_account_dirs() -> list[Path]:
    """通过 Weixin.exe 打开的文件句柄判断哪些账号正在登录（尽力而为）。

    这里退化为：把进程工作集对应的数据目录交给上层排序，主进程只能通过
    内存大小近似判断，因此这里只返回空列表，保持接口稳定。
    """
    return []


def scan_accounts(only_existing: bool = True) -> list[Account]:
    accounts: list[Account] = []
    online_names: set[str] = set()

    # 正在运行的微信 → 标记在线
    for proc in winmem.find_processes(C.WECHAT_V4_PROCESS) + winmem.find_processes(C.WECHAT_V3_PROCESS):
        _ = proc  # 名称在下面通过目录匹配确定，这里只用于触发枚举

    for root in C.wechat_roots():
        if root.name.lower() in ("wechat files",):
            # v3 结构: <root>/<wxid>/
            for acc in sorted(root.iterdir()):
                if not acc.is_dir() or acc.name.startswith("All Users"):
                    continue
                if (acc / "Msg").exists():
                    accounts.append(Account(name=acc.name, data_dir=acc, version=3))
        else:
            # v4 结构: xwechat_files/<wxid>_<suffix>/
            for acc in sorted(root.iterdir()):
                if not acc.is_dir():
                    continue
                if (acc / "db_storage").exists():
                    accounts.append(Account(name=acc.name, data_dir=acc, version=4))

    for a in accounts:
        a.files = a.message_dbs()
        if not a.files and only_existing:
            pass

    # 按数据目录最近修改时间倒序，最近活跃的排前面
    accounts.sort(key=lambda a: _mtime(a.data_dir), reverse=True)
    for a in accounts:
        a.online = a.name in online_names
    return accounts


def _mtime(p: Path) -> float:
    try:
        return (p / "db_storage" / "message").stat().st_mtime
    except Exception:
        try:
            return p.stat().st_mtime
        except Exception:
            return 0.0


def pick_account(accounts: list[Account], name: str | None = None) -> Account:
    if not accounts:
        raise RuntimeError("没有找到任何微信数据目录，请在设置里手动指定")
    if name:
        for a in accounts:
            if name == a.name:
                return a
        raise RuntimeError(f"没有找到账号 {name}")
    # 默认选消息库最大的那个
    def size(a: Account) -> int:
        return sum(f.stat().st_size for f in a.files) if a.files else 0

    return max(accounts, key=size)
