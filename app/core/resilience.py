# app/core/resilience.py
"""
Resilience & Fault Tolerance: Implements retry decorators with exponential
backoff and jitter to protect external API calls from transient failures.
"""

import random
import logging
import asyncio
from functools import wraps

logger = logging.getLogger(__name__)


def retry_on_failure(retries: int = 3, delay: float = 1.0, backoff: float = 2.0, jitter: bool = True):
    """
    Decorator to retry asynchronous functions on exceptions.
    Uses exponential backoff and optional randomized jitter.
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            current_delay = delay
            for attempt in range(retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    # If this was the last attempt, raise the error
                    if attempt == retries - 1:
                        logger.error(
                            f"❌ All {retries} retry attempts failed in '{func.__name__}': {e}"
                        )
                        raise e
                    
                    # Calculate backoff delay with jitter
                    actual_delay = current_delay
                    if jitter:
                        actual_delay += random.uniform(0, 0.5 * current_delay)

                    logger.warning(
                        f"⚠️ Transient exception in '{func.__name__}' "
                        f"(Attempt {attempt + 1}/{retries}): {e}. "
                        f"Retrying in {actual_delay:.2f}s..."
                    )
                    await asyncio.sleep(actual_delay)
                    current_delay *= backoff
        return wrapper
    return decorator


def retry_on_failure_sync(retries: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """
    Decorator to retry synchronous functions with exponential backoff.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            current_delay = delay
            import time
            for attempt in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == retries - 1:
                        logger.error(
                            f"❌ All {retries} sync retry attempts failed in '{func.__name__}': {e}"
                        )
                        raise e
                    logger.warning(
                        f"⚠️ Transient exception in '{func.__name__}' "
                        f"(Attempt {attempt + 1}/{retries}): {e}. "
                        f"Retrying in {current_delay:.2f}s..."
                    )
                    time.sleep(current_delay)
                    current_delay *= backoff
        return wrapper
    return decorator
