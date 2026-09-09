"""Check fibre health using the combined NPAW API."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

from common.models import ModuleResult
from common.api_http import ProxyConfigurationError, exception_details, request, safe_url


async def check(fibre_id: str) -> ModuleResult:
    """GET the combined response with internetId; no authentication required."""
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    api_url = os.getenv("NPAW_API_URL", "").strip()
    if not api_url:
        return ModuleResult(
            source="npaw", fibre_id=fibre_id, status="N/A",
            details={"message": "NPAW_API_URL is not configured"},
        )

    diagnostics = {"stage": "url_configuration", "method": "GET", "endpoint": safe_url(api_url)}
    failure = None
    try:
        url = httpx.URL(api_url).copy_set_param("internetId", fibre_id)
        response = await request("GET", url, diagnostics=diagnostics)
        diagnostics["stage"] = "http_response"
        response.raise_for_status()
        diagnostics["stage"] = "response_json"
        payload = response.json()
        diagnostics["stage"] = "response_validation"
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object")
        api_status = payload.get("status")
        if not isinstance(api_status, str) or api_status.strip().lower() not in (
            "normal", "abnormal"
        ):
            raise ValueError("Expected status Normal or Abnormal")
        status = api_status.strip().lower()
        # The workbook's Normal example omits errors.
        errors = payload.get("errors", [] if status == "normal" else None)
        if not isinstance(errors, list):
            raise ValueError("Expected an errors array")
        return ModuleResult(
            source="npaw", fibre_id=fibre_id, status=status,
            details={"errors": errors},
        )
    except ProxyConfigurationError as exc:
        message = str(exc)
        failure = exc
    except httpx.HTTPStatusError as exc:
        message = f"NPAW API returned HTTP {exc.response.status_code}"
        failure = exc
    except httpx.TimeoutException as exc:
        message = "NPAW API request timed out"
        failure = exc
    except httpx.RequestError as exc:
        message = "NPAW API request failed"
        failure = exc
    except (ValueError, httpx.InvalidURL) as exc:
        message = "Invalid NPAW URL or response; expected Normal/Abnormal status and an errors array"
        failure = exc

    diagnostics["causes"] = exception_details(failure)

    return ModuleResult(
        source="npaw", fibre_id=fibre_id, status="error",
        details={"error": message, "diagnostics": diagnostics},
    )
