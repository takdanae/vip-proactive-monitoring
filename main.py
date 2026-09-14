"""vip-proactive-monitoring — main orchestrator.

Reads fibre records from Cloudflare D1, runs enabled monitoring modules
(airnet, onesense, npaw) in parallel for each fibre, aggregates an overall
status per fibre, and writes the consolidated result to output/summary.json.

Overall status logic (per fibre):
  - "normal"   : all active modules returned "normal"
  - "abnormal" : at least one active module returned "abnormal"
  - "error"    : at least one active module returned "error"
  - "unknown"  : insufficient coverage, unless abnormal or error takes priority
  - Modules returning "N/A" are excluded from aggregation.
  - If ALL modules return "N/A" (no data), overall = "unknown".
"""

from __future__ import annotations

import asyncio
import argparse
import time
import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from common.d1_fibres import FibreRecord, read_fibre_list
from common.d1_logs import cleanup_logs, persist_run_log

from modules.airnet.utils import setup_logging
from common.models import ModuleResult
from common.teams_notification import build_html_message, send_notification, NotificationError
import modules.airnet as airnet
import modules.onesense as onesense
import modules.npaw as npaw

OUTPUT_DIR = Path(__file__).parent / "output"
TZ_BKK = timezone(timedelta(hours=7))
TEST_SCENARIOS = (
    "smart7-offline", "smart7-recent-offlines", "smart7-error",
    "npaw-errors", "npaw-long-metadata", "npaw-oversize-metadata", "npaw-error",
    "onesense-alerts", "onesense-error", "all-errors", "all-abnormal",
)

logger = logging.getLogger("vip-proactive-monitoring")

# Status priority: higher index = higher severity (wins in aggregation).
_STATUS_PRIORITY: dict[str, int] = {
    "N/A":      0,
    "normal":   1,
    "unknown":  2,
    "abnormal": 3,
    "error":    4,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def configure_console_output() -> None:
    """Allow the Unicode monitoring summary on legacy Windows console encodings."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

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


def save_test_summary(summary: dict) -> Path:
    """Write an isolated test-scenario summary without replacing live output."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_file = OUTPUT_DIR / "test-summary.json"
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
        if fibre[flag] > 0:
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


def build_test_summary(scenario: str) -> dict:
    """Create one safe, representative result set for Teams message testing."""
    fibre_id = "TEST-001"
    fibre: FibreRecord = {
        "name": f"TEST — {scenario}", "fibre_id": fibre_id,
        "mesh": 1, "playbox": 1,
    }
    modules = {
        "airnet": ModuleResult("airnet", fibre_id, "normal", {"online_status": "Online"}),
        "onesense": ModuleResult("onesense", fibre_id, "normal", {"alerts": []}),
        "npaw": ModuleResult("npaw", fibre_id, "normal", {"errors": []}),
    }

    if scenario == "smart7-offline":
        modules["airnet"] = ModuleResult("airnet", fibre_id, "abnormal", {"online_status": "Offline"})
    elif scenario == "smart7-recent-offlines":
        modules["airnet"] = ModuleResult(
            "airnet", fibre_id, "abnormal", {"online_status": "Online", "recent_offline_rows": 3},
        )
    elif scenario == "smart7-error":
        modules["airnet"] = ModuleResult("airnet", fibre_id, "error", {"error": "Test retrieval failure"})
    elif scenario == "npaw-errors":
        modules["npaw"] = ModuleResult("npaw", fibre_id, "abnormal", {"errors": [
            {"task": "VDO Error", "error": [{
                "errorCode": "ERROR_CODE_IO_NETWORK_CONNECTION_FAILED (2001)",
                "description": "HttpDataSourceException", "title": "Test video",
                "device": "Android", "occurredAt": "2026-09-11 10:00:00",
            }]},
            {"task": "App Error", "error": [{
                "errorName": "TEST_APP_ERROR", "description": "Test application error",
                "metadata": "test scenario", "count": 1,
            }]},
        ]})
    elif scenario == "npaw-long-metadata":
        modules["npaw"] = ModuleResult("npaw", fibre_id, "abnormal", {"errors": [{
            "task": "App Error", "error": [{
                "errorName": "TEST_LONG_METADATA", "description": "Test long metadata",
                "metadata": "LONG_METADATA_" * 500, "count": 1,
            }],
        }]})
    elif scenario == "npaw-oversize-metadata":
        modules["npaw"] = ModuleResult("npaw", fibre_id, "abnormal", {"errors": [{
            "task": "App Error", "error": [{
                "errorName": "TEST_OVERSIZE_METADATA", "description": "Test oversize metadata",
                "metadata": "OVERSIZE_METADATA_" * 2000, "count": 1,
            }],
        }]})
    elif scenario == "npaw-error":
        modules["npaw"] = ModuleResult("npaw", fibre_id, "error", {"error": "Test retrieval failure"})
    elif scenario == "onesense-alerts":
        modules["onesense"] = ModuleResult("onesense", fibre_id, "abnormal", {"alerts": [
            {
                "alert_id": 10001, "type": "HIGH_LATENCY", "severity": "MINOR",
                "target": "test.example.com", "isp": "Test ISP", "status": "OPEN",
                "start_time": "2026-09-11T10:00:00+07:00", "end_time": None,
                "duration_seconds": 170, "detail": {"summary": "Test high latency", "threshold_ms": 200},
            },
            {
                "alert_id": 10002, "type": "PING_TIMEOUT", "severity": "MAJOR",
                "target": "test.example.com", "isp": "Test ISP", "status": "CLOSED",
                "start_time": "2026-09-11T09:40:00+07:00", "end_time": "2026-09-11T09:45:00+07:00",
                "duration_seconds": 300, "detail": {"summary": "Test ping timeout"},
            },
        ]})
    elif scenario == "onesense-error":
        modules["onesense"] = ModuleResult(
            "onesense", fibre_id, "error", {"error": "OneSense API request timed out", "alerts": []},
        )
    elif scenario == "all-errors":
        modules = {
            source: ModuleResult(source, fibre_id, "error", {"error": "Test retrieval failure"})
            for source in ("airnet", "onesense", "npaw")
        }
    elif scenario == "all-abnormal":
        modules = {
            "airnet": ModuleResult(
                "airnet", fibre_id, "abnormal", {"online_status": "Online", "recent_offline_rows": 3},
            ),
            "npaw": ModuleResult("npaw", fibre_id, "abnormal", {"errors": [{
                "task": "App Error", "error": [{
                    "errorName": "TEST_ALL_ABNORMAL", "description": "Test app error",
                    "metadata": "test scenario", "count": 1,
                }],
            }]}),
            "onesense": ModuleResult("onesense", fibre_id, "abnormal", {"alerts": [{
                "alert_id": 10003, "type": "HIGH_LATENCY", "severity": "MINOR",
                "target": "test.example.com", "isp": "Test ISP", "status": "OPEN",
                "start_time": "2026-09-11T10:00:00+07:00", "end_time": None,
                "duration_seconds": 170, "detail": {"summary": "Test high latency"},
            }]}),
        }
    else:
        raise ValueError(f"Unknown test scenario: {scenario}")

    summary = build_summary([fibre], [[modules[name] for name in ("airnet", "onesense", "npaw")]], datetime.now(tz=TZ_BKK))
    summary["testScenario"] = scenario
    return summary


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_once() -> int:
    """Run cleanup and monitoring, preserving delivery after log failures."""
    failed = False
    try:
        cleanup_logs()
    except ValueError as exc:
        logger.error('Log cleanup failed: %s', exc)
        failed = True
    try:
        fibres = read_fibre_list()
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    logger.info("เริ่ม vip-proactive-monitoring — %d fibres", len(fibres))
    summary = asyncio.run(run(fibres))
    summary['htmlMessage'] = build_html_message(summary)

    output_file = save_summary(summary)
    logger.info("บันทึก summary เสร็จแล้วที่: %s", output_file)

    print_summary(summary)
    print(f"📄 Summary saved to: {output_file}\n")


    try:
        persist_run_log(summary)
    except ValueError as exc:
        logger.error('Run log persistence failed: %s', exc)
        failed = True

    try:
        if summary['htmlMessage']:
            asyncio.run(send_notification(summary['htmlMessage']))
        else:
            logger.info('Teams notification skipped: no abnormal or error services')
    except NotificationError as exc:
        logger.error('%s', exc)
        failed = True
    return int(failed)


def run_test_scenario(scenario: str, *, send_teams: bool = False) -> int:
    """Render one test scenario without using monitoring or D1 services."""
    summary = build_test_summary(scenario)
    summary["htmlMessage"] = build_html_message(summary)
    output_file = save_test_summary(summary)
    logger.info("Saved test scenario summary to: %s", output_file)
    print_summary(summary)
    print(f"📄 Test summary saved to: {output_file}\n")
    if not send_teams:
        logger.info("Teams notification skipped: pass --send-teams to deliver this test scenario")
        return 0
    try:
        asyncio.run(send_notification(summary["htmlMessage"]))
    except NotificationError as exc:
        logger.error("%s", exc)
        return 1
    return 0


def next_quarter_hour(now: datetime) -> datetime:
    """Return the strictly next quarter-hour boundary."""
    return now.replace(second=0, microsecond=0) + timedelta(minutes=15 - now.minute % 15)


def run_scheduled(*, now=None, sleep=time.sleep) -> None:
    """Run sequentially; elapsed boundaries never queue additional runs."""
    now = now or (lambda: datetime.now(TZ_BKK))
    while True:
        current = now()
        target = next_quarter_hour(current)
        logger.info('Next monitoring run: %s', target.isoformat())
        while current < target:
            sleep((target - current).total_seconds())
            current = now()
        try:
            if run_once():
                logger.error('Monitoring cycle completed with failures')
        except Exception:
            logger.error('Monitoring cycle failed unexpectedly; continuing at next boundary')


def main(argv: list[str] | None = None) -> None:
    """Run once by default, or remain active with --cron."""
    parser = argparse.ArgumentParser(description='VIP proactive monitoring')
    parser.add_argument('--cron', action='store_true', help='Run at each next 15-minute boundary (Bangkok time)')
    parser.add_argument('--test-scenario', choices=TEST_SCENARIOS,
                        help='Render a safe simulated Teams notification without live monitoring')
    parser.add_argument('--send-teams', action='store_true',
                        help='Deliver a --test-scenario notification using POWER_AUTOMATE_URL')
    args = parser.parse_args(argv)
    if args.send_teams and not args.test_scenario:
        parser.error('--send-teams requires --test-scenario')
    if args.cron and args.test_scenario:
        parser.error('--cron cannot be used with --test-scenario')
    configure_console_output()
    setup_logging()
    try:
        if args.test_scenario:
            if run_test_scenario(args.test_scenario, send_teams=args.send_teams):
                sys.exit(1)
        elif args.cron:
            run_scheduled()
        elif run_once():
            sys.exit(1)
    except KeyboardInterrupt:
        logger.info('Monitoring stopped')


if __name__ == "__main__":
    main()
