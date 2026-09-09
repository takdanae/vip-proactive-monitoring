"""Query module — fills customer ID, triggers search, and checks online status."""

import logging
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from modules.airnet.utils import retry

logger = logging.getLogger("airnet-scraper")

# Selectors
_CUSTOMER_INPUT = "#dashBoardActionForm\\:customerIdInq"
_QUERY_BUTTON   = "#dashBoardActionForm\\:button-query"
_STATUS_ELEM    = "#dashBoardActionForm\\:online_status-val"


@retry(max_attempts=3, delay=2.0)
async def query_customer(page: Page, customer_number: str) -> str:
    """
    Enter a 10-digit customer number, click Query, and return online status.

    Args:
        page: Playwright Page object (must already be on the dashboard).
        customer_number: 10-digit string to query.

    Returns:
        Status string from the page — e.g. "Online" or "Offline".

    Raises:
        ValueError: If customer_number is not exactly 10 digits.
        PlaywrightTimeout: If the status element is not found after query.
    """
    if not customer_number.isdigit() or len(customer_number) != 10:
        raise ValueError(
            f"customer_number ต้องเป็นตัวเลข 10 หลัก — ได้รับ: '{customer_number}'"
        )

    logger.info("กรอกหมายเลข customer: %s", customer_number)
    await page.fill(_CUSTOMER_INPUT, customer_number)

    logger.info("คลิกปุ่ม Query (AJAX)")
    await page.click(_QUERY_BUTTON)

    try:
        logger.info("รอผลลัพธ์ query โหลด...")
        await page.wait_for_selector(_STATUS_ELEM, timeout=30_000)
    except PlaywrightTimeout as e:
        raise PlaywrightTimeout(
            "Query ล้มเหลว: ไม่พบ status element หลัง 30 วินาที"
        ) from e

    status_text = (await page.text_content(_STATUS_ELEM) or "").strip()
    logger.info("สถานะ: %s", status_text)
    return status_text
