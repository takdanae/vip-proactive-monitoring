"""Retrieve OneSense service alerts through the shared API transport."""

import os
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

from common.api_http import ProxyConfigurationError, request
from common.models import ModuleResult


def _timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('Timezone required')
    return parsed


def _validate_page(payload, fibre_id, offset, as_of):
    if not isinstance(payload, dict) or payload.get('status') not in ('normal', 'abnormal', 'unknown'):
        raise ValueError('Invalid status')
    if payload.get('service_name') != fibre_id or payload.get('range') != '15m':
        raise ValueError('Unexpected service or range')
    for key in ('alert_count', 'offset', 'limit'):
        if type(payload.get(key)) is not int or payload[key] < 0:
            raise ValueError('Invalid page metadata')
    if payload['offset'] != offset or payload['limit'] != 50:
        raise ValueError('Unexpected page metadata')
    for key in ('as_of', 'from', 'to'):
        _timestamp(payload[key])
    if as_of is not None and payload['as_of'] != as_of:
        raise ValueError('Changed time window')
    if type(payload.get('has_more')) is not bool or not isinstance(payload.get('alerts'), list):
        raise ValueError('Invalid pagination or alerts')
    if payload['status'] != 'abnormal' and (payload['alert_count'] or payload['alerts']):
        raise ValueError('Inconsistent alert status')
    if payload['status'] == 'abnormal' and payload['alert_count'] == 0:
        raise ValueError('Missing alert count')
    for alert in payload['alerts']:
        if not isinstance(alert, dict) or type(alert.get('alert_id')) is not int:
            raise ValueError('Invalid alert ID')
        for key in ('service_name', 'target', 'agent_id', 'type', 'severity', 'status'):
            if not isinstance(alert.get(key), str) or not alert[key]:
                raise ValueError('Invalid alert field')
        if alert['service_name'] != fibre_id or alert['type'] not in ('PING_TIMEOUT', 'HIGH_LATENCY') or alert['status'] not in ('OPEN', 'CLOSED'):
            raise ValueError('Unexpected alert')
        _timestamp(alert['start_time'])
        if alert.get('end_time') is not None:
            _timestamp(alert['end_time'])
        for key in ('duration_seconds', 'overlap_seconds'):
            if type(alert.get(key)) not in (int, float) or alert[key] < 0:
                raise ValueError('Invalid duration')
        if not isinstance(alert.get('detail'), dict) or not isinstance(alert.get('traceroute'), dict):
            raise ValueError('Invalid alert details')


async def check(fibre_id: str) -> ModuleResult:
    load_dotenv(Path(__file__).resolve().parents[2] / '.env')
    api_url = os.getenv('ONESENSE_API_URL', '').strip()
    api_key = os.getenv('ONESENSE_API_KEY', '').strip()
    details = {'service_name': fibre_id, 'range': '15m', 'alerts': [], 'incomplete': True}
    result = ModuleResult(source='onesense', fibre_id=fibre_id, status='error', details=details)
    if not api_url or not api_key:
        details['error'] = 'OneSense API URL or key is not configured'
        return result
    try:
        url = httpx.URL(api_url)
        if url.scheme not in ('http', 'https') or not url.host or url.username or url.password:
            raise ValueError('Invalid endpoint')
        # Replace example query parameters with the supported request parameters.
        url = url.copy_with(query=None, fragment=None)
        offset, as_of = 0, None
        seen = set()
        statuses = []
        while True:
            params = {'service_name': fibre_id, 'range': '15m', 'limit': 50, 'offset': offset}
            if as_of is not None:
                params['as_of'] = as_of
            response = await request('GET', url.copy_with(params=params), headers={'x-api-key': api_key})
            if response.status_code != 200:
                suffix = ' (service_not_found)' if response.status_code == 404 else ''
                details['error'] = f'OneSense API returned HTTP {response.status_code}{suffix}'
                return result
            payload = response.json()
            _validate_page(payload, fibre_id, offset, as_of)
            as_of = payload['as_of']
            statuses.append(payload['status'])
            for key in ('as_of', 'from', 'to', 'alert_count', 'coverage', 'reason'):
                if key in payload:
                    details[key] = payload[key]
            for alert in payload['alerts']:
                if alert['alert_id'] not in seen:
                    seen.add(alert['alert_id'])
                    details['alerts'].append(alert)
            # Keep valid fetched incidents even if continuation metadata is broken.
            next_offset = payload.get('next_offset')
            if not payload['has_more']:
                if next_offset is not None:
                    raise ValueError('Unexpected next offset')
                break
            if type(next_offset) is not int or not offset < next_offset <= 10000:
                raise ValueError('Non-progressing pagination')
            offset = next_offset
        if len(seen) < details['alert_count']:
            raise ValueError('Incomplete alert set')
        result.status = max(statuses, key={'normal': 0, 'unknown': 1, 'abnormal': 2}.get)
        details['incomplete'] = False
    except httpx.TimeoutException:
        details['error'] = 'OneSense API request timed out'
    except (httpx.HTTPError, ProxyConfigurationError):
        details['error'] = 'OneSense API request failed'
    except (ValueError, TypeError, KeyError, httpx.InvalidURL):
        details['error'] = 'Invalid OneSense URL, response, or incomplete pagination'
    return result
