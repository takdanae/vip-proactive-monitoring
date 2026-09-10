"""Shared async API requests using an explicit proxy or environment proxy configuration."""

import os
import re
from pathlib import Path

import httpx
from dotenv import load_dotenv


def safe_url(value: str | httpx.URL) -> str:
    """Keep the endpoint, excluding URL credentials, query values and fragments."""
    try:
        url = httpx.URL(value)
        return str(url.copy_with(username=None, password=None, query=None, fragment=None))
    except (ValueError, httpx.InvalidURL):
        return "<invalid URL>"


def exception_details(exc: Exception) -> list[dict]:
    """Describe nested network causes without copying credential-bearing URLs."""
    causes = []
    seen = set()
    while exc is not None and id(exc) not in seen and len(causes) < 8:
        seen.add(id(exc))
        message = re.sub(r"https?://[^\s\"'<>]+", lambda m: safe_url(m.group()), str(exc))
        message = re.sub(r"(?i)(authorization|token|api_key|password)(\s*[:=]\s*)[^\s,;]+", r"\1\2<redacted>", message)
        causes.append({"type": type(exc).__name__, "message": message[:1000] or "No exception message provided"})
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return causes


class ProxyConfigurationError(RuntimeError):
    """The configured proxy URL is invalid or unsupported."""


async def request(method: str, url: str | httpx.URL, *, diagnostics: dict | None = None, **kwargs) -> httpx.Response:
    """Use PROXY_URL when set, otherwise use standard environment proxies."""
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    proxy = os.getenv("PROXY_URL", "").strip() or None
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics.update(method=method, endpoint=safe_url(url), routing="proxy" if proxy else "environment",
                       connect_timeout_seconds=15, read_timeout_seconds=60)
    if proxy:
        diagnostics["stage"] = "proxy_configuration"
        try:
            parsed = httpx.URL(proxy)
            if parsed.scheme not in ("http", "https") or not parsed.host:
                raise ValueError("Unsupported proxy URL")
            httpx.Proxy(proxy)
        except (ValueError, httpx.InvalidURL):
            raise ProxyConfigurationError("Invalid PROXY_URL; expected http://host:port or https://host:port") from None
        diagnostics["route"] = safe_url(proxy)
    diagnostics["stage"] = "api_request"
    async with httpx.AsyncClient(
        proxy=proxy, trust_env=not bool(proxy),
        timeout=httpx.Timeout(60.0, connect=15.0),
    ) as client:
        response = await client.request(method, url, **kwargs)
        diagnostics["http_status"] = response.status_code
        return response
