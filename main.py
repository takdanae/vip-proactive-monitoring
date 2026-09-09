"""vip-proactive-monitoring — main orchestrator.

Reads fibre IDs from fibre_list.json, runs enabled monitoring modules
(airnet, onesense, npaw) in parallel for each fibre, aggregates an overall
status per fibre, and writes the consolidated result to output/summary.json.

Overall status logic (per fibre):
  - "normal"   : all active modules returned "normal"
  - "abnormal" : at least one active module returned "abnormal"
  - "error"    : at least one active module returned "error"
  - Modules returning "N/A" are excluded from aggregation.
  - If ALL modules return "N/A" (no data), overall = "unknown".
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import TypedDict

from modules.airnet.utils import setup_logging
from common.models import ModuleResult
import modules.airnet as airnet
import modules.onesense as onesense
import modules.npaw as npaw

OUTPUT_DIR = Path(__file__).parent / "output"
FIBRE_LIST = Path(__file__).parent / "fibre_list.json"
TZ_BKK = timezone(timedelta(hours=7))

logger = logging.getLogger("vip-proactive-monitoring")

# Status priority: higher index = higher severity (wins in aggregation).
_STATUS_PRIORITY: dict[str, int] = {
    "N/A":      0,
    "normal":   1,
    "abnormal": 2,
    "error":    3,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FibreRecord(TypedDict):
    name: str
    fibre_id: str
    mesh: int
    playbox: int


def read_fibre_list(path: Path = FIBRE_LIST) -> list[FibreRecord]:
    """Validate every JSON record before any monitoring requests start."""
    try:
        records = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot load {path.name}: {exc}") from exc
    if not isinstance(records, list) or not records:
        raise ValueError(f"{path.name}: expected a non-empty JSON array")
    seen = set()
    for index, record in enumerate(records, 1):
        label = f"{path.name}: record {index}"
        if not isinstance(record, dict):
            raise ValueError(f"{label}: expected an object")
        for field in ("name", "fibre_id", "mesh", "playbox"):
            if field not in record:
                raise ValueError(f"{label}: missing required field {field}")
        name = record["name"]
        if not isinstance(name, str) or not name or name != name.strip():
            raise ValueError(f"{label}: name must be a non-empty string without surrounding whitespace")
        fid = record["fibre_id"]
        if not isinstance(fid, str) or not fid or fid != fid.strip():
            raise ValueError(f"{label}: fibre_id must be a non-empty string without surrounding whitespace")
        if fid in seen:
            raise ValueError(f"{label}: duplicate fibre_id {fid}")
        seen.add(fid)
        for flag in ("mesh", "playbox"):
            if type(record[flag]) is not int or record[flag] not in (0, 1):
                raise ValueError(f"{label} ({fid}): {flag} must be integer 0 or 1; set the confirmed device flag")
    return records


def aggregate_status(results: list[ModuleResult]) -> str:
    """
    Compute overall status from a list of ModuleResults.

    Active results (status != "N/A") are ranked by severity.
    If no active results exist, returns "unknown".
    """
    active = [r for r in results if r.is_active()]
    if not active:
        return "unknown"
    # Pick highest-severity status.
    return max(active, key=lambda r: _STATUS_PRIORITY.get(r.status, 0)).status


def build_summary(
    fibres: list[FibreRecord],
    all_results: list[list[ModuleResult]],
    started_at: datetime,
) -> dict:
    """Build the full summary dict for JSON output."""
    fibres_summary = []
    status_counts: dict[str, int] = {"normal": 0, "abnormal": 0, "error": 0, "unknown": 0}

    for fibre, results in zip(fibres, all_results):
        overall = aggregate_status(results)
        status_counts[overall] = status_counts.get(overall, 0) + 1

        fibres_summary.append({
            "name": fibre["name"],
            "fibre_id": fibre["fibre_id"],
            "mesh": fibre["mesh"],
            "playbox": fibre["playbox"],
            "overall_status": overall,
            "modules": {r.source: r.to_dict() for r in results},
        })

    return {
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(tz=TZ_BKK).isoformat(),
        "fibre_count": len(fibres),
        "status_counts": status_counts,
        "fibres": fibres_summary,
    }


def save_summary(summary: dict) -> Path:
    """Write summary dict to output/summary.json."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_file = OUTPUT_DIR / "summary.json"
    with output_file.open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return output_file


def print_summary(summary: dict) -> None:
    """Print a human-readable summary table to stdout."""
    status_icon = {
        "normal":   "✅",
        "abnormal": "⚠️ ",
        "error":    "❌",
        "unknown":  "❓",
        "N/A":      "—",
    }

    print("\n" + "═" * 72)
    print("  VIP Proactive Monitoring — Summary")
    print("═" * 72)
    print(f"  Started : {summary['started_at']}")
    print(f"  Finished: {summary['finished_at']}")
    print(f"  Fibres  : {summary['fibre_count']}")
    print()

    # Header row
    print(f"  {'Name':<20} {'Fibre ID':<14} {'Overall':<12} {'Airnet':<12} {'OneSense':<12} {'NPAW':<12}")
    print("  " + "─" * 82)

    for entry in summary["fibres"]:
        name    = entry["name"]
        fid     = entry["fibre_id"]
        overall = entry["overall_status"]
        modules = entry["modules"]

        def fmt(source: str) -> str:
            m = modules.get(source, {})
            s = m.get("status", "—")
            return f"{status_icon.get(s, s)} {s}"

        print(
            f"  {name:<20.20} {fid:<14} "
            f"{status_icon.get(overall, overall)} {overall:<10} "
            f"{fmt('airnet'):<20} "
            f"{fmt('onesense'):<20} "
            f"{fmt('npaw'):<20}"
        )

    print("═" * 72)
    counts = summary["status_counts"]
    print(
        f"  ✅ normal: {counts.get('normal', 0)}  "
        f"⚠️  abnormal: {counts.get('abnormal', 0)}  "
        f"❌ error: {counts.get('error', 0)}  "
        f"❓ unknown: {counts.get('unknown', 0)}"
    )
    print("═" * 72 + "\n")


# ---------------------------------------------------------------------------
# Core async logic
# ---------------------------------------------------------------------------

async def check_fibre(fibre: FibreRecord) -> list[ModuleResult]:
    """Run Airnet and device-enabled modules concurrently."""
    fibre_id = fibre["fibre_id"]
    module_names = ["airnet", "onesense", "npaw"]
    selected = [("airnet", airnet.check)]
    skipped = {}
    for name, flag, checker in [("onesense", "mesh", onesense.check), ("npaw", "playbox", npaw.check)]:
        if fibre[flag] == 1:
            selected.append((name, checker))
        else:
            skipped[name] = ModuleResult(
                source=name, fibre_id=fibre_id, status="N/A",
                details={"message": f"Skipped: {flag}=0"},
            )
    responses = await asyncio.gather(
        *(checker(fibre_id) for _, checker in selected), return_exceptions=True,
    )
    by_name = dict(zip((name for name, _ in selected), responses))
    by_name.update(skipped)
    raw_results = [by_name[name] for name in module_names]
    results: list[ModuleResult] = []

    for name, result in zip(module_names, raw_results):
        if isinstance(result, BaseException):
            logger.error("[%s] fibre %s — unhandled exception: %s", name, fibre_id, result)
            results.append(ModuleResult(
                source=name,
                fibre_id=fibre_id,
                status="error",
                details={"error": str(result)},
            ))
        else:
            results.append(result)

    return results


async def run(fibres: list[FibreRecord]) -> dict:
    """Sequentially check each fibre (enabled modules run in parallel)."""
    started_at = datetime.now(tz=TZ_BKK)
    all_results: list[list[ModuleResult]] = []

    for fibre in fibres:
        results = await check_fibre(fibre)
        all_results.append(results)

    return build_summary(fibres, all_results, started_at)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point — read fibre list and run full monitoring check."""
    setup_logging()
    try:
        fibres = read_fibre_list()
    except ValueError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    logger.info("เริ่ม vip-proactive-monitoring — %d fibres", len(fibres))
    summary = asyncio.run(run(fibres))

    output_file = save_summary(summary)
    logger.info("บันทึก summary เสร็จแล้วที่: %s", output_file)

    print_summary(summary)
    print(f"📄 Summary saved to: {output_file}\n")


if __name__ == "__main__":
    main()
