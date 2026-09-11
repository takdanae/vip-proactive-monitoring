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
_PROTECTED_DETAIL_KEYS = {
    'api_key', 'authorization', 'cookie', 'cookies', 'diagnostics', 'headers',
    'output_file', 'password', 'request', 'response', 'secret', 'token', 'url',
}


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
    if source == 'airnet' and str(details.get('online_status', '')).strip().casefold() == 'offline':
        return 'Router Offline'
    return ' / '.join(str(value) for value in [
        details.get('online_status'),
    ] if value is not None)


def _status(value: str) -> str:
    color = {'normal': 'green', 'abnormal': 'red', 'error': 'red', 'unknown': '#b36b00'}.get(value, 'gray')
    return f'<span style="color:{color}">{escape(value.lower())}</span>'


def _visible_json_value(value: object) -> object:
    """Remove operational data that is not safe to include in notifications."""
    if isinstance(value, dict):
        return {
            key: _visible_json_value(item)
            for key, item in value.items()
            if str(key).casefold() not in _PROTECTED_DETAIL_KEYS
        }
    if isinstance(value, list):
        return [_visible_json_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_visible_json_value(item) for item in value)
    return value


def _pretty_json_html(value: object) -> str:
    """Format structured values safely in the constrained Teams HTML dialect."""
    rendered = json.dumps(_visible_json_value(value), ensure_ascii=False, indent=2, default=str)
    return '<pre style="white-space:pre-wrap;word-break:break-word">' + escape(rendered) + '</pre>'


def _value_html(value: object) -> str:
    return escape(str(value)) if value is not None and value != '' else '-'


def _table_html(headers: tuple[str, ...], rows: list[tuple[object, ...]]) -> str:
    head = ''.join(f'<th>{escape(header)}</th>' for header in headers)
    body = ''.join(
        '<tr>' + ''.join(f'<td>{_value_html(value)}</td>' for value in row) + '</tr>'
        for row in rows
    )
    return '<table><tr>' + head + '</tr>' + body + '</table>'


def _first_present(item: dict, *keys: str) -> object:
    for key in keys:
        value = item.get(key)
        if value is not None and value != '':
            return value
    return None


def _description_or_message(item: dict) -> object:
    values = []
    for key in ('description', 'message'):
        value = item.get(key)
        if value is not None and value != '' and value not in values:
            values.append(value)
    return ' / '.join(str(value) for value in values) if values else None


def _npaw_errors_html(errors: object) -> str:
    """Render known NPAW tasks as compact tables, retaining unknown shapes as JSON."""
    groups = errors if isinstance(errors, list) else [errors]
    fragments = ['<h4>NPAW error details</h4>']
    if not groups:
        return ''.join(fragments) + _pretty_json_html(groups)
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get('error'), list):
            fragments.append(_pretty_json_html(group))
            continue
        task, entries = group.get('task'), group['error']
        if not all(isinstance(entry, dict) for entry in entries):
            fragments.append(_pretty_json_html(group))
        elif task == 'VDO Error':
            rows = [
                (entry.get('occurredAt'),
                 _first_present(entry, 'errorCode', 'errorName'),
                 _description_or_message(entry), entry.get('title'),
                 entry.get('device'))
                for entry in entries
            ]
            fragments.extend(['<h4>Task: VDO Error</h4>', _table_html(
                ('Occurred At', 'Error Code / Name', 'Description / Message', 'Title', 'Device'), rows)])
        elif task == 'App Error':
            rows = [
                (_first_present(entry, 'errorName', 'errorCode'), entry.get('description'),
                 entry.get('metadata'), entry.get('count'))
                for entry in entries
            ]
            fragments.extend(['<h4>Task: App Error</h4>', _table_html(
                ('Error Name / Code', 'Description', 'Metadata', 'Count'), rows)])
        else:
            fragments.append(_pretty_json_html(group))
    return ''.join(fragments)


def _bangkok_datetime(value: object) -> object:
    """Format timezone-aware timestamps without guessing for invalid values."""
    if not isinstance(value, str):
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        return value
    return parsed.astimezone(timezone(timedelta(hours=7))).strftime('%Y-%m-%d %H:%M:%S (UTC+7)')


def _onesense_display_detail(value: object) -> object:
    """Build a display copy, keeping the stored incident payload intact."""
    if isinstance(value, dict):
        return {key: _onesense_display_detail(item) for key, item in value.items()
                if key not in {'packets_lost_unit', 'effective_problem_seconds'}}
    if isinstance(value, list):
        return [_onesense_display_detail(item) for item in value]
    return _bangkok_datetime(value)


def _onesense_detail_html(detail: dict) -> str:
    rendered = json.dumps(_visible_json_value(_onesense_display_detail(detail)),
                          ensure_ascii=False, indent=2, default=str)
    # Plain HTML avoids clients turning <pre> into a numbered code editor.
    lines = []
    for line in rendered.split('\n'):
        indentation = len(line) - len(line.lstrip(' '))
        lines.append('&nbsp;' * indentation + escape(line[indentation:]))
    return ('<div style="font-family:monospace;white-space:normal;overflow-wrap:anywhere;word-break:break-word">'
            + '<br>'.join(lines) + '</div>')


def _duration_label(seconds: int | float | None) -> str | None:
    """Show completed minutes, omitting empty day/hour components."""
    if seconds is None:
        return None
    if seconds == 0:
        return '0 minutes'
    if seconds < 60:
        return 'Less than 1 minute'
    days, minutes = divmod(int(seconds // 60), 1440)
    hours, minutes = divmod(minutes, 60)
    return ' '.join(f'{amount} {unit}' + ('s' if amount != 1 else '')
                    for amount, unit in ((days, 'day'), (hours, 'hour'), (minutes, 'minute'))
                    if amount)


def _alert_html(alert: dict) -> str:
    """Render the requested OneSense incident fields without exposing traceroutes."""
    fields = [
        ('Type', alert.get('type')), ('Severity', alert.get('severity')),
        ('Target', alert.get('target')), ('ISP', alert.get('isp')),
        ('Incident state', alert.get('status')), ('Start', _bangkok_datetime(alert.get('start_time'))),
        ('End', _bangkok_datetime(alert.get('end_time'))),
        ('Duration', _duration_label(alert.get('duration_seconds'))),
    ]
    rows = ''.join(
        f'<tr><td>{escape(label)}</td><td>{_value_html(value)}</td></tr>'
        for label, value in fields
    )
    detail = alert.get('detail')
    detail_html = _onesense_detail_html(detail) if isinstance(detail, dict) else '-'
    return ('<table><tr><th>Field</th><th>Value</th></tr>' + rows
            + f'<tr><td>Detail</td><td>{detail_html}</td></tr></table>')


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
            if source == 'npaw' and module['status'] != 'error':
                extra += _npaw_errors_html(module.get('details', {}).get('errors', []))
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
