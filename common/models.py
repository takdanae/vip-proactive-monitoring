"""Shared data models for vip-proactive-monitoring.

All three modules (airnet, onesense, npaw) return a `ModuleResult`
so that the main orchestrator can aggregate them uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Literal

TZ_BKK = timezone(timedelta(hours=7))

# Status values used across all modules.
StatusLiteral = Literal["normal", "abnormal", "unknown", "error", "N/A"]


@dataclass
class ModuleResult:
    """Result returned by each monitoring module.

    Attributes:
        source:     Name of the module — "airnet", "onesense", or "npaw".
        fibre_id:   The fibre/customer number that was checked.
        status:     Summary status:
                      "normal"   — service is healthy / within expected range.
                      "abnormal" — service is degraded or has a fault.
                      "unknown"  — monitoring coverage is insufficient.
                      "error"    — the module itself failed to retrieve data.
                      "N/A"      — module is not yet implemented / intentionally skipped.
        details:    Free-form dict with module-specific data (e.g. online_status, row_count).
        checked_at: Timestamp of the check (Bangkok time).
    """

    source: str
    fibre_id: str
    status: StatusLiteral
    details: dict = field(default_factory=dict)
    checked_at: datetime = field(default_factory=lambda: datetime.now(tz=TZ_BKK))

    def is_active(self) -> bool:
        """Return True if this result carries meaningful data (not N/A)."""
        return self.status != "N/A"

    def to_dict(self) -> dict:
        """Serialize to a plain dict suitable for JSON output."""
        return {
            "source": self.source,
            "fibre_id": self.fibre_id,
            "status": self.status,
            "details": self.details,
            "checked_at": self.checked_at.isoformat(),
        }
