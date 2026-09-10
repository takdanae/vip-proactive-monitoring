"""Load and validate the monitoring input from Cloudflare D1."""

import asyncio
import os
from pathlib import Path
from typing import TypedDict

import httpx
from dotenv import load_dotenv

from common import api_http


class FibreRecord(TypedDict):
    name: str
    fibre_id: str
    mesh: int
    playbox: int


def validate_fibres(records: object) -> list[FibreRecord]:
    """Reject invalid input before any monitoring requests start."""
    if not isinstance(records, list) or not records:
        raise ValueError('D1 fibre_list: expected a non-empty list of records')
    seen = set()
    for index, record in enumerate(records, 1):
        label = f'D1 fibre_list: record {index}'
        if not isinstance(record, dict):
            raise ValueError(f'{label}: expected an object')
        for field in ('name', 'fibre_id', 'mesh', 'playbox'):
            if field not in record:
                raise ValueError(f'{label}: missing required field {field}')
        for field in ('name', 'fibre_id'):
            value = record[field]
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f'{label}: {field} must be a non-empty string without surrounding whitespace')
        if record['fibre_id'] in seen:
            raise ValueError(f'{label}: duplicate fibre_id')
        seen.add(record['fibre_id'])
        for field in ('mesh', 'playbox'):
            if type(record[field]) is not int or record[field] < 0:
                raise ValueError(f'{label}: {field} must be a nonnegative integer')
    return records


def read_fibre_list() -> list[FibreRecord]:
    """Fetch one startup snapshot. Fail closed without a JSON fallback."""
    return validate_fibres(query_d1(
        'SELECT name, fibre_id, mesh, playbox FROM fibre_list ORDER BY fibre_id'
    ))


def query_d1(sql: str, params: list | None = None) -> list:
    """Execute a parameterized query with shared routing and sanitized errors."""
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    keys = ('CLOUDFLARE_ACCOUNT_ID', 'CLOUDFLARE_D1_DATABASE_ID', 'CLOUDFLARE_API_TOKEN')
    values = [os.getenv(key, '').strip() for key in keys]
    missing = [key for key, value in zip(keys, values) if not value]
    if missing:
        raise ValueError('Missing D1 configuration: ' + ', '.join(missing))
    account, database, token = values
    url = f'https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{database}/query'
    try:
        response = asyncio.run(api_http.request(
            'POST', url, timeout=30.0,
            headers={'Authorization': f'Bearer {token}'}, json={
                'sql': sql,
                **({'params': params} if params is not None else {}),
            },
        ))
        response.raise_for_status()
    except api_http.ProxyConfigurationError:
        raise ValueError('D1 request failed: proxy configuration error; check PROXY_URL') from None
    except httpx.HTTPStatusError as exc:
        raise ValueError(f'D1 request failed with HTTP {exc.response.status_code}') from None
    except httpx.RequestError:
        raise ValueError('D1 request failed: network error or timeout') from None
    try:
        payload = response.json()
    except ValueError:
        raise ValueError('D1 returned malformed JSON') from None
    if not isinstance(payload, dict) or payload.get('success') is not True:
        raise ValueError('D1 API reported an unsuccessful query')
    result = payload.get('result')
    if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
        raise ValueError('D1 returned a malformed query result')
    if result[0].get('success') is not True:
        raise ValueError('D1 SQL query failed')
    rows = result[0].get('results')
    if not isinstance(rows, list):
        raise ValueError('D1 returned malformed result rows')
    return rows


if __name__ == '__main__':
    try:
        records = read_fibre_list()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print(f'D1 read successful: {len(records)} validated fibres; monitoring was not started.')
