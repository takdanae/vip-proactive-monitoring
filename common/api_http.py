"""Shared async API requests using the corporate PAC configuration."""

import asyncio
import os
import re
import threading
from pathlib import Path

import httpx
from dotenv import load_dotenv

_PAC_LOCK = threading.Lock()


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
    """The configured PAC could not select a supported route."""


def _evaluate_pac(script: str, url: str) -> str | None:
    try:
        import pacparser

        with _PAC_LOCK:
            pacparser.init()
            try:
                pacparser.parse_pac_string(script)
                result = pacparser.find_proxy(url, httpx.URL(url).host)
            finally:
                pacparser.cleanup()
        # Match the supplied helper: use only the first PAC rule.
        rule = result.split(";", 1)[0].strip()
        if rule.upper() == "DIRECT":
            return None
        parts = rule.split()
        if len(parts) == 2 and parts[0].upper() == "PROXY":
            return "http://" + parts[1]
    except Exception as exc:
        raise ProxyConfigurationError("PAC evaluation failed; check pacparser and the PAC file") from exc
    raise ProxyConfigurationError("Unsupported or empty first PAC rule; expected DIRECT or PROXY")


async def request(method: str, url: str | httpx.URL, *, diagnostics: dict | None = None, **kwargs) -> httpx.Response:
    """Request an API URL through PAC, or environment proxies when unconfigured."""
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    pac_url = os.getenv("API_PAC_URL", "").strip()
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics.update(method=method, endpoint=safe_url(url), routing="pac" if pac_url else "environment",
                       connect_timeout_seconds=15, read_timeout_seconds=60)
    proxy = None
    if pac_url:
        diagnostics.update(stage="pac_download", pac_endpoint=safe_url(pac_url), pac_timeout_seconds=15)
        try:
            # The intranet PAC server must be reached directly.
            async with httpx.AsyncClient(trust_env=False, timeout=15.0) as client:
                pac_response = await client.get(pac_url)
                diagnostics["pac_http_status"] = pac_response.status_code
                pac_response.raise_for_status()
            diagnostics["stage"] = "pac_evaluation"
            proxy = await asyncio.to_thread(_evaluate_pac, pac_response.text, str(url))
            diagnostics["route"] = safe_url(proxy) if proxy else "DIRECT"
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise ProxyConfigurationError("Could not download API_PAC_URL") from exc
    diagnostics["stage"] = "api_request"
    async with httpx.AsyncClient(
        proxy=proxy, trust_env=not bool(pac_url),
        timeout=httpx.Timeout(60.0, connect=15.0),
    ) as client:
        response = await client.request(method, url, **kwargs)
        diagnostics["http_status"] = response.status_code
        return response
