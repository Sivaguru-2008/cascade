"""Message bus + shared state.

Two interchangeable backends, chosen by CASCADE_MODE:
  services -> RedisBus  (Redis Streams + hashes; works with Redis >= 6.2)
  single   -> MemoryBus (asyncio, one process; the s390x fallback when Redis is unavailable)

Streams carry one JSON document per entry (field "d"). Consumer groups give each service its own
cursor. Back-pressure: publishers wait while the slowest consumer group lags too far behind.
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from collections import deque
from typing import Any

STREAM_DISPENSE = "stream:dispense"
STREAM_ALERTS = "stream:alerts"
STREAM_SIGNALS = "stream:signals"
STREAM_CONTROL = "stream:control"

# Consumer groups that must keep up with each stream (used for back-pressure and trimming).
GROUPS: dict[str, tuple[str, ...]] = {
    STREAM_DISPENSE: ("scorer", "signals"),
    STREAM_CONTROL: ("edge",),
}

Message = tuple[str, dict[str, Any]]


class Bus(ABC):
    max_lag: int = 20000

    # ----- streams -----
    @abstractmethod
    async def publish_many(self, stream: str, items: list[dict[str, Any]]) -> None: ...

    async def publish(self, stream: str, item: dict[str, Any]) -> None:
        await self.publish_many(stream, [item])

    @abstractmethod
    async def ensure_group(self, stream: str, group: str) -> None: ...

    @abstractmethod
    async def read_group(self, stream: str, group: str, consumer: str, count: int = 200,
                         block_ms: int = 1000) -> list[Message]: ...

    @abstractmethod
    async def ack(self, stream: str, group: str, ids: list[str]) -> None: ...

    @abstractmethod
    async def tail(self, stream: str, last_id: str, count: int = 200, block_ms: int = 1000) -> list[Message]:
        """Broadcast read (no group); last_id='$' means only new entries."""

    @abstractmethod
    async def tail_many(self, cursors: dict[str, str], count: int = 200,
                        block_ms: int = 1000) -> list[tuple[str, str, dict[str, Any]]]:
        """Broadcast read over several streams; returns (stream, id, data). Cursors must be concrete ids."""

    @abstractmethod
    async def last_id(self, stream: str) -> str: ...

    @abstractmethod
    async def lag(self, stream: str) -> int:
        """Entries published but not yet read by the slowest required group."""

    async def wait_for_capacity(self, stream: str) -> None:
        while await self.lag(stream) > self.max_lag:
            await asyncio.sleep(0.05)

    async def wait_until_drained(self, stream: str, timeout_s: float = 120.0) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            if await self.lag(stream) <= 0 and await self.pending(stream) == 0:
                return True
            await asyncio.sleep(0.05)
        return False

    @abstractmethod
    async def pending(self, stream: str) -> int: ...

    @abstractmethod
    async def trim(self, stream: str) -> None: ...

    # ----- key/value -----
    @abstractmethod
    async def hgetall_many(self, keys: list[str]) -> list[dict[str, str]]: ...

    @abstractmethod
    async def hset_many(self, items: dict[str, dict[str, str]]) -> None: ...

    @abstractmethod
    async def hincrby_many(self, ops: list[tuple[str, str, int]]) -> None: ...

    @abstractmethod
    async def get(self, key: str) -> str | None: ...

    @abstractmethod
    async def set(self, key: str, value: str) -> None: ...

    @abstractmethod
    async def incr(self, key: str, n: int = 1) -> int: ...

    @abstractmethod
    async def keys(self, prefix: str) -> list[str]: ...

    @abstractmethod
    async def delete_prefix(self, prefix: str) -> int: ...

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


def _enc(item: dict[str, Any]) -> str:
    return json.dumps(item, default=str, separators=(",", ":"))


# =====================================================================================
class _MemStream:
    def __init__(self) -> None:
        self.entries: deque[tuple[int, dict[str, Any]]] = deque()
        self.next_seq = 1
        self.groups: dict[str, int] = {}
        self.inflight: dict[str, int] = {}
        self.cond = asyncio.Condition()

    def base(self) -> int:
        return self.entries[0][0] if self.entries else self.next_seq

    def slice_from(self, seq: int, count: int) -> list[tuple[int, dict[str, Any]]]:
        if not self.entries:
            return []
        offset = max(0, seq - self.base())
        return [self.entries[i] for i in range(offset, min(len(self.entries), offset + count))]


class MemoryBus(Bus):
    """Single-process bus. Entries are trimmed once every required group has read them."""

    keep_tail = 2000

    def __init__(self) -> None:
        self._streams: dict[str, _MemStream] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._kv: dict[str, str] = {}
        self._any = asyncio.Condition()
        self._version = 0

    def _s(self, stream: str) -> _MemStream:
        if stream not in self._streams:
            self._streams[stream] = _MemStream()
        return self._streams[stream]

    async def publish_many(self, stream: str, items: list[dict[str, Any]]) -> None:
        s = self._s(stream)
        for item in items:
            s.entries.append((s.next_seq, json.loads(_enc(item))))
            s.next_seq += 1
        async with s.cond:
            s.cond.notify_all()
        self._version += 1
        async with self._any:
            self._any.notify_all()
        if len(s.entries) > self.keep_tail * 4:
            await self.trim(stream)

    async def ensure_group(self, stream: str, group: str) -> None:
        s = self._s(stream)
        s.groups.setdefault(group, s.base())
        s.inflight.setdefault(group, 0)

    async def read_group(self, stream: str, group: str, consumer: str, count: int = 200,
                         block_ms: int = 1000) -> list[Message]:
        await self.ensure_group(stream, group)
        s = self._s(stream)
        if s.groups[group] >= s.next_seq:
            async with s.cond:
                try:
                    await asyncio.wait_for(s.cond.wait_for(lambda: s.groups[group] < s.next_seq),
                                           block_ms / 1000)
                except TimeoutError:
                    return []
        batch = s.slice_from(s.groups[group], count)
        if batch:
            s.groups[group] = batch[-1][0] + 1
            s.inflight[group] = s.inflight.get(group, 0) + len(batch)
        return [(str(seq), data) for seq, data in batch]

    async def ack(self, stream: str, group: str, ids: list[str]) -> None:
        s = self._s(stream)
        s.inflight[group] = max(0, s.inflight.get(group, 0) - len(ids))

    async def tail(self, stream: str, last_id: str, count: int = 200, block_ms: int = 1000) -> list[Message]:
        s = self._s(stream)
        start = s.next_seq if last_id == "$" else int(last_id) + 1
        if start >= s.next_seq:
            async with s.cond:
                try:
                    await asyncio.wait_for(s.cond.wait_for(lambda: start < s.next_seq), block_ms / 1000)
                except TimeoutError:
                    return []
        start = max(start, s.base())
        return [(str(seq), data) for seq, data in s.slice_from(start, count)]

    async def tail_many(self, cursors: dict[str, str], count: int = 200,
                        block_ms: int = 1000) -> list[tuple[str, str, dict[str, Any]]]:
        def collect() -> list[tuple[str, str, dict[str, Any]]]:
            out: list[tuple[str, str, dict[str, Any]]] = []
            for name, cur in cursors.items():
                s = self._s(name)
                start = max(int(cur) + 1, s.base())
                out += [(name, str(seq), data) for seq, data in s.slice_from(start, count)]
            return out

        got = collect()
        if got:
            return got
        seen = self._version
        async with self._any:
            try:
                await asyncio.wait_for(self._any.wait_for(lambda: self._version != seen), block_ms / 1000)
            except TimeoutError:
                return []
        return collect()

    async def last_id(self, stream: str) -> str:
        return str(self._s(stream).next_seq - 1)

    async def lag(self, stream: str) -> int:
        s = self._s(stream)
        required = GROUPS.get(stream, ())
        cursors = [s.groups.get(g, s.base()) for g in required]
        return (s.next_seq - min(cursors)) if cursors else 0

    async def pending(self, stream: str) -> int:
        s = self._s(stream)
        return sum(s.inflight.get(g, 0) for g in GROUPS.get(stream, ()))

    async def trim(self, stream: str) -> None:
        s = self._s(stream)
        required = GROUPS.get(stream, ())
        floor = min((s.groups.get(g, s.base()) for g in required), default=s.next_seq)
        floor = min(floor, s.next_seq - self.keep_tail)
        while s.entries and s.entries[0][0] < floor:
            s.entries.popleft()

    async def hgetall_many(self, keys: list[str]) -> list[dict[str, str]]:
        return [dict(self._hashes.get(k, {})) for k in keys]

    async def hset_many(self, items: dict[str, dict[str, str]]) -> None:
        for k, mapping in items.items():
            self._hashes.setdefault(k, {}).update(mapping)

    async def hincrby_many(self, ops: list[tuple[str, str, int]]) -> None:
        for k, f, n in ops:
            h = self._hashes.setdefault(k, {})
            h[f] = str(int(h.get(f, "0")) + n)

    async def get(self, key: str) -> str | None:
        return self._kv.get(key)

    async def set(self, key: str, value: str) -> None:
        self._kv[key] = value

    async def incr(self, key: str, n: int = 1) -> int:
        v = int(self._kv.get(key, "0")) + n
        self._kv[key] = str(v)
        return v

    async def keys(self, prefix: str) -> list[str]:
        return [k for k in list(self._hashes) + list(self._kv) if k.startswith(prefix)]

    async def delete_prefix(self, prefix: str) -> int:
        doomed = [k for k in self._hashes if k.startswith(prefix)] + [k for k in self._kv if k.startswith(prefix)]
        for k in doomed:
            self._hashes.pop(k, None)
            self._kv.pop(k, None)
        return len(doomed)


# =====================================================================================
class RedisBus(Bus):
    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis

        self._r = aioredis.from_url(url, decode_responses=True)
        self._groups_ready: set[tuple[str, str]] = set()

    async def ping(self) -> bool:
        try:
            return bool(await self._r.ping())
        except Exception:
            return False

    async def close(self) -> None:
        await self._r.aclose() if hasattr(self._r, "aclose") else await self._r.close()

    async def publish_many(self, stream: str, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        pipe = self._r.pipeline(transaction=False)
        for item in items:
            pipe.xadd(stream, {"d": _enc(item)})
        pipe.incrby(f"published:{stream}", len(items))
        await pipe.execute()

    async def ensure_group(self, stream: str, group: str) -> None:
        if (stream, group) in self._groups_ready:
            return
        from redis.exceptions import ResponseError

        try:
            await self._r.xgroup_create(stream, group, id="0", mkstream=True)
            # A new group starts at the beginning of what is still in the stream.
            await self._r.set(f"consumed:{stream}:{group}",
                              int(await self._r.get(f"published:{stream}") or 0) - await self._r.xlen(stream))
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._groups_ready.add((stream, group))

    async def read_group(self, stream: str, group: str, consumer: str, count: int = 200,
                         block_ms: int = 1000) -> list[Message]:
        await self.ensure_group(stream, group)
        resp = await self._r.xreadgroup(group, consumer, {stream: ">"}, count=count, block=block_ms)
        out: list[Message] = []
        for _, entries in resp or []:
            for msg_id, fields in entries:
                out.append((msg_id, json.loads(fields["d"])))
        if out:
            await self._r.incrby(f"consumed:{stream}:{group}", len(out))
        return out

    async def ack(self, stream: str, group: str, ids: list[str]) -> None:
        if ids:
            await self._r.xack(stream, group, *ids)

    async def tail(self, stream: str, last_id: str, count: int = 200, block_ms: int = 1000) -> list[Message]:
        if last_id == "$":
            last_id = await self.last_id(stream)
        resp = await self._r.xread({stream: last_id}, count=count, block=block_ms)
        out: list[Message] = []
        for _, entries in resp or []:
            for msg_id, fields in entries:
                out.append((msg_id, json.loads(fields["d"])))
        return out

    async def tail_many(self, cursors: dict[str, str], count: int = 200,
                        block_ms: int = 1000) -> list[tuple[str, str, dict[str, Any]]]:
        resp = await self._r.xread(dict(cursors), count=count, block=block_ms)
        out: list[tuple[str, str, dict[str, Any]]] = []
        for name, entries in resp or []:
            for msg_id, fields in entries:
                out.append((name, msg_id, json.loads(fields["d"])))
        return out

    async def last_id(self, stream: str) -> str:
        entries = await self._r.xrevrange(stream, count=1)
        return entries[0][0] if entries else "0-0"

    async def lag(self, stream: str) -> int:
        required = GROUPS.get(stream, ())
        if not required:
            return 0
        published = int(await self._r.get(f"published:{stream}") or 0)
        consumed = [int(await self._r.get(f"consumed:{stream}:{g}") or 0) for g in required]
        return published - min(consumed)

    async def pending(self, stream: str) -> int:
        from redis.exceptions import ResponseError

        total = 0
        for g in GROUPS.get(stream, ()):
            try:
                info = await self._r.xpending(stream, g)
                total += int(info.get("pending", 0))
            except ResponseError:
                continue
        return total

    async def trim(self, stream: str) -> None:
        """Drop entries every required group has received (keeps unacknowledged ones)."""
        from redis.exceptions import ResponseError

        try:
            groups = {g["name"]: g for g in await self._r.xinfo_groups(stream)}
        except ResponseError:
            return
        floors: list[str] = []
        for name in GROUPS.get(stream, ()):
            g = groups.get(name)
            if g is None:
                return  # a required consumer has not joined yet: keep everything
            floor = g["last-delivered-id"]
            if int(g.get("pending", 0)) > 0:
                summary = await self._r.xpending(stream, name)
                if summary.get("min"):
                    floor = summary["min"]
            floors.append(floor)
        if floors:
            def key(i: str) -> tuple[int, int]:
                a, _, b = i.partition("-")
                return int(a), int(b or 0)

            await self._r.xtrim(stream, minid=min(floors, key=key), approximate=True)

    async def hgetall_many(self, keys: list[str]) -> list[dict[str, str]]:
        if not keys:
            return []
        pipe = self._r.pipeline(transaction=False)
        for k in keys:
            pipe.hgetall(k)
        return list(await pipe.execute())

    async def hset_many(self, items: dict[str, dict[str, str]]) -> None:
        if not items:
            return
        pipe = self._r.pipeline(transaction=False)
        for k, mapping in items.items():
            if mapping:
                pipe.hset(k, mapping=mapping)
        await pipe.execute()

    async def hincrby_many(self, ops: list[tuple[str, str, int]]) -> None:
        if not ops:
            return
        pipe = self._r.pipeline(transaction=False)
        for k, f, n in ops:
            pipe.hincrby(k, f, n)
        await pipe.execute()

    async def get(self, key: str) -> str | None:
        return await self._r.get(key)

    async def set(self, key: str, value: str) -> None:
        await self._r.set(key, value)

    async def incr(self, key: str, n: int = 1) -> int:
        return int(await self._r.incrby(key, n))

    async def keys(self, prefix: str) -> list[str]:
        return [k async for k in self._r.scan_iter(match=f"{prefix}*", count=1000)]

    async def delete_prefix(self, prefix: str) -> int:
        doomed = await self.keys(prefix)
        for i in range(0, len(doomed), 500):
            await self._r.delete(*doomed[i:i + 500])
        return len(doomed)


# =====================================================================================
_memory_bus: MemoryBus | None = None


def get_bus() -> Bus:
    """RedisBus in services mode; one shared MemoryBus per process in single mode."""
    from common.config import get_settings

    global _memory_bus
    s = get_settings()
    if s.mode == "single":
        if _memory_bus is None:
            _memory_bus = MemoryBus()
        return _memory_bus
    return RedisBus(s.redis_url)


def reset_memory_bus() -> None:
    global _memory_bus
    _memory_bus = None
