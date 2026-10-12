"""Thin adapter for web rescue endpoints."""
from __future__ import annotations

try:
    from scripts.rescue_login import (
        count_rescues_from_log,
        rescue_and_persist,
        _proxy_raw as rescue_proxy_raw,
    )

    RESCUE_READY = True
except Exception:  # noqa: BLE001
    count_rescues_from_log = None  # type: ignore[misc, assignment]
    rescue_and_persist = None  # type: ignore[misc, assignment]
    rescue_proxy_raw = None  # type: ignore[misc, assignment]
    RESCUE_READY = False
