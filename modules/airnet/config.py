"""Configuration loader — reads credentials and URL from .env file."""

import os
import sys
import logging
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger("airnet-scraper")


@dataclass(frozen=True)
class ScraperConfig:
    """Immutable configuration for the scraper."""
    base_url: str
    username: str
    password: str


def load_config(env_path: Path | None = None) -> ScraperConfig:
    """
    Load configuration from .env file.

    Args:
        env_path: Optional explicit path to .env file.
                  Defaults to .env in the project root.

    Returns:
        ScraperConfig with validated values.

    Raises:
        SystemExit: If any required config is missing.
    """
    if env_path is None:
        # modules/airnet/config.py → project root is 3 levels up
        env_path = Path(__file__).resolve().parent.parent.parent / ".env"

    if not env_path.exists():
        logger.error("ไม่พบไฟล์ .env ที่ %s — กรุณาสร้างจาก .env.example", env_path)
        sys.exit(1)

    load_dotenv(env_path)

    base_url = os.getenv("BASE_URL", "").strip()
    username = os.getenv("USERNAME", "").strip()
    password = os.getenv("PASSWORD", "").strip()

    missing = []
    if not base_url:
        missing.append("BASE_URL")
    if not username:
        missing.append("USERNAME")
    if not password:
        missing.append("PASSWORD")

    if missing:
        logger.error("ค่า config ที่ขาด: %s — กรุณาตั้งค่าใน .env", ", ".join(missing))
        sys.exit(1)

    logger.info("โหลด config สำเร็จ — URL: %s", base_url)
    return ScraperConfig(base_url=base_url, username=username, password=password)
