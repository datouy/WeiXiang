"""分析用的紧凑消息模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class Rec:
    ts: int
    seq: int
    sender: str
    kind: str
    text: str
    local_type: int = 1
    mentions: tuple[str, ...] = ()
    quote_name: str = ""
    quote_text: str = ""
    is_self: bool = False

    @property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.ts)

    @property
    def day(self) -> str:
        return datetime.fromtimestamp(self.ts).strftime("%Y-%m-%d")

    @property
    def hm(self) -> str:
        return datetime.fromtimestamp(self.ts).strftime("%H:%M")

    @property
    def hh(self) -> int:
        return datetime.fromtimestamp(self.ts).hour
