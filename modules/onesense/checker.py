"""OneSense monitoring module — REST API integration (future).

This module is a stub that returns N/A until the OneSense API credentials
and endpoints are configured and the integration is implemented.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from common.models import ModuleResult

logger = logging.getLogger("onesense")

TZ_BKK = timezone(timedelta(hours=7))


async def check(fibre_id: str) -> ModuleResult:
    """
    Check a fibre's status via the OneSense API.

    NOTE: Not yet implemented — returns N/A until API integration is ready.
          To implement: set ONESENSE_API_URL and ONESENSE_API_KEY in .env
          and replace this stub with common.api_http.request() to use the
          shared API_PAC_URL proxy routing for the OneSense endpoint.

    Args:
        fibre_id: The fibre/customer number to check.

    Returns:
        ModuleResult with status="N/A".
    """
    logger.debug("[onesense] fibre %s — stub, returning N/A", fibre_id)
    return ModuleResult(
        source="onesense",
        fibre_id=fibre_id,
        status="N/A",
        details={"message": "Not implemented yet — awaiting API integration"},
        checked_at=datetime.now(tz=TZ_BKK),
    )
