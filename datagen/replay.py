"""Time-ordered replay of generated raw events (section 4.6).

Reading and pacing live here; tokenising and publishing live in edge/pharmacy_edge.py, because only
the edge holds EDGE_SECRET. `python -m datagen.replay ...` is an alias for the edge CLI.
"""

from __future__ import annotations

import asyncio
import csv
import gzip
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from common.config import get_settings

RAW_COLUMNS = ("event_id", "ts", "pharmacy_id", "patient_id", "prescriber_id", "age_band", "sex", "drug_id",
               "atc_code", "atc_l4", "atc_l3", "qty", "days_supply", "dose_mg")


def default_events_path() -> Path:
    return get_settings().generated_dir / "events.csv.gz"


def iter_raw(path: Path | None = None, start: int = 0, stop: int | None = None) -> Iterator[dict[str, str]]:
    """Yield raw rows [start, stop) in file order (the generator writes them sorted by ts)."""
    path = path or default_events_path()
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", newline="", encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f)):
            if i < start:
                continue
            if stop is not None and i >= stop:
                return
            yield row


@dataclass
class Pacer:
    """Maps simulated time to wall time. mode: max | days_per_sec | events_per_sec."""

    mode: str = "days_per_sec"
    rate: float = 1.0
    _t0_wall: float | None = None
    _t0_sim: datetime | None = None
    _n: int = 0

    @classmethod
    def parse(cls, spec: str) -> Pacer:
        spec = spec.strip()
        if spec == "max":
            return cls("max", 0.0)
        name, _, value = spec.partition("=")
        if name not in ("days_per_sec", "events_per_sec") or not value:
            raise ValueError("speed must be 'max', 'days_per_sec=<x>' or 'events_per_sec=<x>'")
        return cls(name, float(value))

    def reset(self) -> None:
        self._t0_wall = self._t0_sim = None
        self._n = 0

    def delay_for(self, ts: datetime) -> float:
        """Seconds to wait before emitting an event with simulated time ts."""
        now = time.monotonic()
        if self.mode == "max":
            return 0.0
        if self._t0_wall is None:
            self._t0_wall, self._t0_sim = now, ts
        if self.mode == "events_per_sec":
            target = self._t0_wall + self._n / self.rate
            self._n += 1
        else:
            sim_days = (ts - self._t0_sim).total_seconds() / 86400.0  # type: ignore[operator]
            target = self._t0_wall + sim_days / self.rate
        return max(0.0, target - now)

    async def wait(self, ts: datetime) -> None:
        delay = self.delay_for(ts)
        if delay > 0:
            await asyncio.sleep(delay)


async def run_demo_script(edge: Any, path: Path | None = None, warmup_only: bool = False) -> None:
    """Play the curated demo sequence (implemented in phase 8)."""
    raise NotImplementedError("demo script is built in phase 8")


def main() -> None:
    from edge.pharmacy_edge import main as edge_main

    edge_main()


if __name__ == "__main__":
    main()
