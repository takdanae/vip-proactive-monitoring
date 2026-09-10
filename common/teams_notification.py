"""Render one bounded HTML notification and deliver it through Power Automate."""

from html import escape
import json
import logging
import os
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

from common.api_http import ProxyConfigurationError, request

logger = logging.getLogger(__name__)
MAX_HTML_BYTES = 24 * 1024
_SERVICES = [('airnet', 'Smart7'), ('npaw', 'NPAW'), ('onesense', 'OneSense')]


class NotificationError(RuntimeError):
    """A sanitized delivery failure suitable for CLI logging."""


def run_status(summary: dict) -> str:
    """Aggregate fibre statuses without changing monitoring results."""
    priority = {'normal': 0, 'unknown': 1, 'abnormal': 2, 'error': 3}
    statuses = [f['overall_status'] for f in summary['fibres']]
    return max(statuses, key=lambda s: priority.get(s, 1)) if statuses else 'unknown'


def _detail(source: str, module: dict) -> str:
    """Select display details; retrieval diagnostics never leave the summary."""
    if module['status'] == 'error':
        return 'Retrieval failed'
    details = module.get('details', {})
    if source == 'onesense':
        return str(details.get('reason', ''))
    if source == 'npaw':
        return 'Errors: ' + json.dumps(details.get('errors', []), ensure_ascii=False)
    return ' / '.join(str(value) for value in [
        details.get('online_status'),
    ] if value is not None)


def _status(value: str) -> str:
    color = {'normal': 'green', 'abnormal': 'red', 'error': 'red', 'unknown': '#b36b00'}.get(value, 'gray')
    return f'<span style="color:{color}">{escape(value.lower())}</span>'


def _alert_html(alert: dict) -> str:
    fields = [('Alert ID', alert.get('alert_id')), ('Type', alert.get('type')),
              ('Severity', alert.get('severity')), ('Target', alert.get('target')),
              ('Agent', alert.get('agent_id')), ('ISP', alert.get('isp')),
              ('Incident state', alert.get('status')), ('Start', alert.get('start_time')),
              ('End', alert.get('end_time') or 'Still open'),
              ('Duration', f"{alert.get('duration_seconds')} s"),
              ('Overlap', f"{alert.get('overlap_seconds')} s")]
    detail = alert.get('detail', {})
    fields.append(('Summary', detail.get('summary')))
    for key, label, unit in (
        ('last_avg_ping_ms', 'Latest latency', 'ms'),
        ('threshold_ms', 'Latency threshold', 'ms'),
        ('worst_avg_ping_ms', 'Worst latency', 'ms'),
        ('max_packets_lost', 'Maximum loss', '%'),
        ('effective_problem_seconds', 'Effective problem time', 's'),
        ('last_metric_at', 'Latest metric at', ''),
        ('metric_scope', 'Metric scope', ''),
    ):
        if detail.get(key) is not None:
            fields.append((label, f'{detail[key]} {unit}'.strip()))
    return '<p>' + '<br>'.join(f'{label}: {escape(str(value))}' for label, value in fields if value is not None) + '</p>'


def _blocks(summary: dict, alert_limits: dict) -> list[str]:
    blocks = []
    for index, fibre in enumerate(summary['fibres']):
        rows = []
        for source, label in _SERVICES:
            module = fibre['modules'].get(source, {})
            if module.get('status') not in ('abnormal', 'error', 'unknown'):
                continue
            detail = _detail(source, module)
            extra = ''
            if source == 'onesense':
                details = module.get('details', {})
                # Only checker-generated, fixed failure descriptions are exposed.
                if module['status'] == 'error':
                    message = details.get('error', '')
                    safe_messages = (
                        'OneSense API URL or key is not configured',
                        'OneSense API request timed out', 'OneSense API request failed',
                        'Invalid OneSense URL, response, or incomplete pagination',
                    )
                    if message in safe_messages or (isinstance(message, str) and re.fullmatch(r'OneSense API returned HTTP [0-9]{3}( \(service_not_found\))?', message)):
                        detail += ': ' + message
                if details.get('incomplete'):
                    extra += '<p>Incomplete results; some alerts may be unavailable.</p>'
                alerts = details.get('alerts', [])
                keep = alert_limits.get(index, len(alerts))
                extra += ''.join(_alert_html(alert) for alert in alerts[:keep])
                if keep < len(alerts):
                    extra += f'<p>{len(alerts) - keep} alerts omitted; full results in summary.json</p>'
            rows.append(
                f'<tr><td>{label}</td><td>Status: {_status(module["status"])}'
                + (f'<p>{escape(detail)}</p>' if detail else '') + extra + '</td></tr>'
            )
        if not rows:
            continue
        blocks.append(
            f'Name: {escape(str(fibre["name"]))}<br>'
            f'Fibre ID: {escape(str(fibre["fibre_id"]))}<br>'
            f'Status: {_status(str(fibre["overall_status"]))}<br><br>'
            '<table><tr><th>Service</th><th>Detail</th></tr>'
            + ''.join(rows) + '</table><br>'
        )

    return blocks


def build_html_message(summary: dict) -> str:
    """Keep complete alerts and fibre blocks within the Teams HTML budget."""
    alert_limits = {i: len(f['modules'].get('onesense', {}).get('details', {}).get('alerts', []))
                    for i, f in enumerate(summary['fibres'])}
    blocks = _blocks(summary, alert_limits)
    if not blocks:
        return ''
    finished = datetime.fromisoformat(summary['finished_at']).astimezone(timezone(timedelta(hours=7)))
    header = ('<b>VIP proactive monitoring</b><br>'
              + finished.strftime('%Y-%m-%d %H:%M:%S') + ' (UTC+7)<br>'
              + f'Status: {_status(run_status(summary))}<br><br>')
    def size():
        return len((header + ''.join(blocks)).encode('utf-8'))
    # Remove entire alert entries, newest first retained, before dropping fibres.
    for index in reversed(alert_limits):
        while alert_limits[index] and size() > MAX_HTML_BYTES:
            alert_limits[index] -= 1
            blocks = _blocks(summary, alert_limits)
    if size() <= MAX_HTML_BYTES:
        return header + ''.join(blocks)
    total = len(blocks)
    used = len(header.encode('utf-8')) + sum(len(block.encode('utf-8')) for block in blocks)
    for kept in range(total - 1, -1, -1):
        used -= len(blocks[kept].encode('utf-8'))
        notice = f'<i>{total - kept} fibres omitted; full results in summary.json</i><br>'
        if used + len(notice.encode('utf-8')) <= MAX_HTML_BYTES:
            return header + ''.join(blocks[:kept]) + notice
    return ''  # Only reachable with an artificially tiny test budget.


async def send_notification(html_message: str) -> bool:
    """POST once; return False when disabled, True when the flow accepts it."""
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    url = os.getenv('POWER_AUTOMATE_URL', '').strip()
    if not url:
        logger.info('Teams notification disabled: POWER_AUTOMATE_URL is blank')
        return False
    # HTTPX logs full signed request URLs at INFO. Suppress its request records
    # for delivery, restoring logging even when the request fails or is cancelled.
    http_logger = logging.getLogger('httpx')
    def hide_request(record: logging.LogRecord) -> bool:
        return False
    http_logger.addFilter(hide_request)
    try:
        response = await request('POST', url, json={'htmlMessage': html_message})
    except (httpx.HTTPError, httpx.InvalidURL, ProxyConfigurationError, ValueError) as exc:
        # Never include the signed URL, response body, or raw exception text.
        raise NotificationError(f'Power Automate request failed ({type(exc).__name__}); not retried') from None
    finally:
        http_logger.removeFilter(hide_request)
    if not 200 <= response.status_code < 300:
        raise NotificationError(f'Power Automate returned HTTP {response.status_code}; not retried')
    logger.info('Power Automate accepted the Teams notification (HTTP %d)', response.status_code)
    return True
