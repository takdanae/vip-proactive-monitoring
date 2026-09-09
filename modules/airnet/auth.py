"""Auth module — handles login flow."""

import logging
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from modules.airnet.config import ScraperConfig
from modules.airnet.utils import retry

logger = logging.getLogger("airnet-scraper")

# Selectors
_USERNAME_INPUT = "#loginF\\:userId"
_PASSWORD_INPUT = "#loginF\\:password"
_LOGIN_BUTTON   = "#loginF\\:Login"
_DASHBOARD_ELEM = "#dashBoardActionForm\\:customerIdInq"


@retry(max_attempts=3, delay=2.0)
async def login(page: Page, config: ScraperConfig) -> None:
    """
    Navigate to the login page, fill credentials, and submit.

    Args:
        page: Playwright Page object.
        config: ScraperConfig with base_url, username, password.

    Raises:
        PlaywrightTimeout: If the dashboard element is not found after login.
    """
    logger.info("เปิดหน้า Login: %s", config.base_url)
    await page.goto(config.base_url, wait_until="domcontentloaded")

    logger.info("กรอก username")
    await page.fill(_USERNAME_INPUT, config.username)

    logger.info("กรอก password")
    await page.fill(_PASSWORD_INPUT, config.password)

    logger.info("คลิกปุ่ม Login (AJAX)")
    await page.click(_LOGIN_BUTTON)

    try:
        logger.info("รอ dashboard โหลด...")
        await page.wait_for_selector(_DASHBOARD_ELEM, timeout=30_000)
        logger.info("Login สำเร็จ — dashboard โหลดแล้ว")
    except PlaywrightTimeout as e:
        raise PlaywrightTimeout(
            "Login ล้มเหลว: ไม่พบ dashboard element หลัง 30 วินาที"
        ) from e
