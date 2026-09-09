"""Airnet module entry point — wraps the Playwright scraper into a check() function."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.async_api import async_playwright

from modules.airnet.config import load_config
from modules.airnet.auth import login
from modules.airnet.query import query_customer
from modules.airnet.extractor import extract_table
from common.models import ModuleResult

logger = logging.getLogger("airnet-scraper")

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output"
TZ_BKK = timezone(timedelta(hours=7))

_OFFLINE_TIME_COL = "Offline Time"
_OFFLINE_WINDOW_MINUTES = 30
_OFFLINE_COUNT_THRESHOLD = 5
_OFFLINE_TIME_FMT = "%d/%m/%Y %H:%M"


def _check_offline_time_abnormal(rows: list[dict], now: datetime) -> bool:
    """
    Return True if ≥ _OFFLINE_COUNT_THRESHOLD rows in *rows* have an
    "Offline Time" value that falls within the last _OFFLINE_WINDOW_MINUTES
    minutes relative to *now*.

    Rows with a missing, empty, or un-parseable "Offline Time" are skipped.

    Args:
        rows: Extracted table rows (list of dicts keyed by column header).
        now:  Reference timestamp (Bangkok-tz aware).

    Returns:
        True if the offline-time condition is met, False otherwise.
    """
    # Truncate to minute precision: the scraped "Offline Time" has no seconds,
    # so we align the cutoff to the same resolution to make boundary inclusive.
    now_trunc = now.replace(second=0, microsecond=0)
    cutoff = now_trunc - timedelta(minutes=_OFFLINE_WINDOW_MINUTES)
    count = 0
    for row in rows:
        raw = row.get(_OFFLINE_TIME_COL, "").strip()
        if not raw:
            continue
        try:
            offline_dt = datetime.strptime(raw, _OFFLINE_TIME_FMT).replace(tzinfo=TZ_BKK)
        except ValueError:
            logger.debug("[airnet] ไม่สามารถ parse Offline Time: %r", raw)
            continue
        if cutoff <= offline_dt <= now_trunc:
            count += 1
    result = count >= _OFFLINE_COUNT_THRESHOLD
    if result:
        logger.info(
            "[airnet] พบ offline ใน %d นาทีล่าสุด %d แถว (threshold=%d) → abnormal",
            _OFFLINE_WINDOW_MINUTES,
            count,
            _OFFLINE_COUNT_THRESHOLD,
        )
    return result


def _determine_status(online_status: str, rows: list[dict], now: datetime) -> str:
    """
    Determine ModuleResult status from Airnet data.

    Abnormal if EITHER condition is true:
      1. The query page reports the line as Offline.
      2. ≥ 5 rows in the Historical Usage table have an Offline Time within
         the last 30 minutes.

    Args:
        online_status: Status string from the query page (e.g. "Online", "Offline").
        rows:          Extracted Historical Usage table rows.
        now:           Reference timestamp used for the 30-minute window.

    Returns:
        "abnormal" or "normal".
    """
    if online_status.lower() == "offline":
        logger.info("[airnet] online_status=Offline → abnormal")
        return "abnormal"
    if _check_offline_time_abnormal(rows, now):
        return "abnormal"
    return "normal"


def _save_detail(fibre_id: str, online_status: str, rows: list[dict]) -> Path:
    """
    Save per-fibre detailed scrape result to output/result_<fibre_id>.json.

    This preserves the original per-fibre JSON output format.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_file = OUTPUT_DIR / f"result_{fibre_id}.json"

    payload = {
        "query_number": fibre_id,
        "online_status": online_status,
        "scraped_at": datetime.now(tz=TZ_BKK).isoformat(),
        "row_count": len(rows),
        "data": rows,
    }

    with output_file.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    return output_file


async def check(
    fibre_id: str,
    headless: bool = True,
    max_rows: int | None = None,
) -> ModuleResult:
    """
    Check a fibre's status via the Airnet web portal.

    Opens a Playwright browser, logs in, queries the fibre ID, extracts the
    Historical Usage table, saves a detailed JSON file, and returns a
    `ModuleResult` with the aggregated status.

    Args:
        fibre_id:  10-digit customer/fibre number.
        headless:  Whether to run the browser in headless mode (default True).
        max_rows:  Max rows to extract from the usage table (None = all).

    Returns:
        ModuleResult with source="airnet" and status "normal"/"abnormal"/"error".
    """
    checked_at = datetime.now(tz=TZ_BKK)
    config = load_config()

    logger.info(
        "[airnet] เริ่ม check — fibre: %s | mode: %s",
        fibre_id,
        "headless" if headless else "headed",
    )

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=headless)
            context = await browser.new_context()
            page = await context.new_page()

            try:
                await login(page, config)
                online_status = await query_customer(page, fibre_id)
                rows = await extract_table(page, max_rows=max_rows)
            finally:
                await browser.close()

        output_file = _save_detail(fibre_id, online_status, rows)
        logger.info("[airnet] fibre %s → %s | rows: %d | saved: %s",
                    fibre_id, online_status, len(rows), output_file)

        return ModuleResult(
            source="airnet",
            fibre_id=fibre_id,
            status=_determine_status(online_status, rows, checked_at),
            details={
                "online_status": online_status,
                "row_count": len(rows),
                "output_file": str(output_file),
            },
            checked_at=checked_at,
        )

    except Exception as exc:
        logger.exception("[airnet] fibre %s — เกิดข้อผิดพลาด: %s", fibre_id, exc)
        return ModuleResult(
            source="airnet",
            fibre_id=fibre_id,
            status="error",
            details={"error": str(exc)},
            checked_at=checked_at,
        )
