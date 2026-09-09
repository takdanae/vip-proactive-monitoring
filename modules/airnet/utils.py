"""Utility functions: logging setup and retry decorator."""

import logging
import functools
import time
from typing import TypeVar, Callable, Any

F = TypeVar("F", bound=Callable[..., Any])

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure and return the root logger for the scraper."""
    logging.basicConfig(level=level, format=LOG_FORMAT, datefmt=DATE_FORMAT)
    logger = logging.getLogger("airnet-scraper")
    logger.setLevel(level)
    return logger


def retry(max_attempts: int = 3, delay: float = 2.0, backoff: float = 2.0):
    """
    Retry decorator with exponential backoff.

    Args:
        max_attempts: Maximum number of retry attempts.
        delay: Initial delay between retries (seconds).
        backoff: Multiplier applied to delay after each retry.
    """
    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            logger = logging.getLogger("airnet-scraper")
            current_delay = delay
            last_exception = None

            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_attempts:
                        logger.warning(
                            "Attempt %d/%d failed for '%s': %s — retrying in %.1fs",
                            attempt, max_attempts, func.__name__, e, current_delay,
                        )
                        time.sleep(current_delay)
                        current_delay *= backoff
                    else:
                        logger.error(
                            "All %d attempts failed for '%s': %s",
                            max_attempts, func.__name__, e,
                        )

            raise last_exception  # type: ignore[misc]

        return wrapper  # type: ignore[return-value]

    return decorator
