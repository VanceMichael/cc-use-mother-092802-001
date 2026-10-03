"""只追加、带哈希链的事件台账。

所有领域事实都以事件表达；事件只能追加，不能修改或删除（R-LOG-1）。
每条事件记录前一条事件的哈希，任何篡改都会使校验失败（R-LOG-2）。
服务停机后仅凭台账文件即可重放出全部状态（R-RESUME）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS_PREV = "0" * 64


def utc_now() -> str:
    """当前 UTC 时间，ISO-8601 字符串。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    """与键顺序无关、无空白的规范 JSON，用于哈希与指纹。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(seq: int, ts: str, event_type: str, payload: dict[str, Any], prev_hash: str) -> str:
    body = canonical_json(
        {"seq": seq, "ts": ts, "type": event_type, "payload": payload, "prev": prev_hash}
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Event:
    seq: int
    ts: str
    type: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "type": self.type,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Event":
        return cls(
            seq=raw["seq"],
            ts=raw["ts"],
            type=raw["type"],
            payload=raw["payload"],
            prev_hash=raw["prev_hash"],
            hash=raw["hash"],
        )


class LedgerError(ValueError):
    """台账被破坏或追加违反单调约束。"""


class Ledger:
    """内存中的只追加事件流，可持久化为 JSON 数组文件。"""

    def __init__(self, events: Iterable[Event] | None = None) -> None:
        self._events: list[Event] = []
        for event in events or []:
            self._adopt(event)

    def __len__(self) -> None:
        return len(self._events)

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    @property
    def events(self) -> list[Event]:
        return list(self._events)

    @property
    def head_hash(self) -> str:
        return self._events[-1].hash if self._events else GENESIS_PREV

    def _adopt(self, event: Event) -> None:
        expected_seq = len(self._events) + 1
        if event.seq != expected_seq:
            raise LedgerError(f"事件序号必须连续：期望{expected_seq}，实际{event.seq}")
        expected_prev = self.head_hash
        if event.prev_hash != expected_prev:
            raise LedgerError(f"事件{event.seq}前哈希不衔接")
        expected_hash = digest(event.seq, event.ts, event.type, event.payload, event.prev_hash)
        if event.hash != expected_hash:
            raise LedgerError(f"事件{event.seq}哈希无效，载荷可能被改动")
        if self._events and event.ts < self._events[-1].ts:
            raise LedgerError(f"事件{event.seq}时间戳早于前一事件")
        self._events.append(event)

    def append(self, event_type: str, payload: dict[str, Any], ts: str | None = None) -> Event:
        seq = len(self._events) + 1
        ts = ts or utc_now()
        if self._events and ts < self._events[-1].ts:
            raise LedgerError(f"事件{seq}时间戳早于前一事件")
        prev = self.head_hash
        event = Event(
            seq=seq,
            ts=ts,
            type=event_type,
            payload=payload,
            prev_hash=prev,
            hash=digest(seq, ts, event_type, payload, prev),
        )
        self._events.append(event)
        return event

    def validate(self) -> None:
        """完整校验序号连续性、哈希链与时间戳单调性，失败即抛 LedgerError。"""
        prev = GENESIS_PREV
        last_ts: str | None = None
        for index, event in enumerate(self._events, start=1):
            if event.seq != index:
                raise LedgerError(f"事件序号必须连续：位置{index}为{event.seq}")
            if event.prev_hash != prev:
                raise LedgerError(f"事件{event.seq}前哈希断链")
            if digest(event.seq, event.ts, event.type, event.payload, event.prev_hash) != event.hash:
                raise LedgerError(f"事件{event.seq}哈希校验失败")
            if last_ts is not None and event.ts < last_ts:
                raise LedgerError(f"事件{event.seq}时间戳回退")
            prev = event.hash
            last_ts = event.ts

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps([event.to_dict() for event in self._events], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "Ledger":
        raw = json.loads(path.read_text(encoding="utf-8"))
        ledger = cls()
        for item in raw:
            ledger._adopt(Event.from_dict(item))
        return ledger
