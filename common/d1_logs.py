"""Persist monitoring results and enforce the D1 log retention period."""

import json
from datetime import datetime, timedelta, timezone

from common.d1_fibres import query_d1


def utc_timestamp(value: datetime) -> str:
    """Use one sortable UTC representation for storage and retention."""
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def persist_run_log(summary: dict) -> None:
    """Store one row containing the existing serialized results for each module."""
    created_at = utc_timestamp(datetime.fromisoformat(summary['finished_at']))
    logs = [json.dumps([fibre['modules'][source] for fibre in summary['fibres']], ensure_ascii=False)
            for source in ('airnet', 'npaw', 'onesense')]
    query_d1(
        'INSERT INTO log_table (created_at, log_airnet, log_npaw, log_onesense) VALUES (?, ?, ?, ?)',
        [created_at, *logs],
    )


def cleanup_logs(now: datetime | None = None) -> None:
    """Keep the exact 30-day boundary and delete only older database logs."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=30)
    query_d1('DELETE FROM log_table WHERE created_at < ?', [utc_timestamp(cutoff)])
