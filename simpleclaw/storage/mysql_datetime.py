"""Shared datetime helpers for MySQL-backed storage."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_dt() -> datetime:
    return datetime.now().replace(microsecond=0)


def _ms_to_dt(value: Any, *, nullable: bool = False) -> datetime | None:
    if value is None:
        return None if nullable else _now_dt()
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None if nullable else _now_dt()
        try:
            return datetime.fromisoformat(raw).replace(microsecond=0)
        except ValueError:
            try:
                value = float(raw)
            except ValueError:
                return None if nullable else _now_dt()
    if isinstance(value, (int, float)):
        if value <= 0:
            return None if nullable else _now_dt()
        return datetime.fromtimestamp(float(value) / 1000).replace(microsecond=0)
    return None if nullable else _now_dt()


def _dt_to_ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return int(datetime.fromisoformat(raw).timestamp() * 1000)
        except ValueError:
            try:
                return int(float(raw))
            except ValueError:
                return None
    if isinstance(value, (int, float)):
        return int(value)
    return None
