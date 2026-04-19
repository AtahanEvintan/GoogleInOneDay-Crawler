"""
Async HTTP fetcher with token-bucket rate limiter and concurrency semaphore.

Backpressure mechanisms:
1. Token-bucket rate limiter: configurable N requests/second.
2. asyncio.Semaphore: limits max in-flight HTTP requests.
3. Per-host politeness delay: minimum interval between requests to same domain.

All fetch operations are async coroutines designed to run within the asyncio event loop.
"""

import asyncio
import time
import logging
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)

# Default configuration
DEFAULT_MAX_RATE = 10.0          # requests per second
DEFAULT_MAX_CONCURRENT = 20      # max in-flight HTTP requests
DEFAULT_REQUEST_TIMEOUT = 30     # seconds
DEFAULT_MAX_RESPONSE_SIZE = 5 * 1024 * 1024  # 5 MB
DEFAULT_POLITENESS_DELAY = 1.0   # seconds between requests to same host
USER_AGENT = "GoogleInOneDay-Crawler/2.0"


class TokenBucketRateLimiter:
    """
    Async token-bucket rate limiter.

    Tokens refill at a constant rate. Each fetch consumes one token.
    If the bucket is empty, the caller awaits until a token is available.
    """

    def __init__(self, rate: float = DEFAULT_MAX_RATE):
        self.rate = rate              # tokens per second
        self.max_tokens = rate        # bucket capacity = rate (allows short bursts)
        self._tokens = rate           # start full
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        """Wait until a token is available, then consume it."""
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self.max_tokens, self._tokens + elapsed * self.rate)
                self._last_refill = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

            # No token available — wait for the next one to refill
            wait_time = (1.0 - self._tokens) / self.rate
            await asyncio.sleep(max(0.01, wait_time))

    @property
    def is_throttled(self) -> bool:
        """Check if rate limiter is currently depleted (no tokens available)."""
        return self._tokens < 1.0


class HostPolitenessTracker:
    """
    Track the last request time per host to enforce politeness delay.
    Prevents hammering the same domain too frequently.
    """

    def __init__(self, delay: float = DEFAULT_POLITENESS_DELAY):
        self.delay = delay
        self._last_request: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def wait_for_host(self, url: str):
        """Wait until the politeness delay has passed for this host."""
        host = urlparse(url).netloc
        # Calculate wait and reserve slot inside lock, then sleep outside it
        # so other workers on different hosts are not serialized.
        async with self._lock:
            last = self._last_request.get(host, 0.0)
            now = time.monotonic()
            wait_time = max(0.0, self.delay - (now - last))
            self._last_request[host] = now + wait_time

        if wait_time > 0:
            await asyncio.sleep(wait_time)


class Fetcher:
    """
    Async HTTP fetcher with rate limiting, concurrency control, and politeness.

    Usage:
        fetcher = Fetcher(max_rate=10, max_concurrent=20)
        async with fetcher:
            result = await fetcher.fetch(url)
    """

    def __init__(
        self,
        max_rate: float = DEFAULT_MAX_RATE,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
        max_response_size: int = DEFAULT_MAX_RESPONSE_SIZE,
        politeness_delay: float = DEFAULT_POLITENESS_DELAY,
    ):
        self.request_timeout = request_timeout
        self.max_response_size = max_response_size

        # Backpressure controls
        self.rate_limiter = TokenBucketRateLimiter(rate=max_rate)
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.politeness = HostPolitenessTracker(delay=politeness_delay)

        # Stats
        self.total_fetched = 0
        self.total_errors = 0

        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        timeout = aiohttp.ClientTimeout(total=self.request_timeout)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            connector=aiohttp.TCPConnector(
                limit=100,
                ssl=False,  # Skip SSL verification for broader crawling
            ),
        )
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()
            self._session = None

    async def fetch(self, url: str) -> dict | None:
        """
        Fetch a URL with rate limiting, concurrency control, and politeness.

        Returns dict with keys: url, final_url, status, html, content_type
        Returns None on error (logged but not raised).
        """
        # Acquire rate limiter token
        await self.rate_limiter.acquire()

        # Acquire concurrency semaphore slot
        async with self.semaphore:
            # Enforce per-host politeness delay
            await self.politeness.wait_for_host(url)

            try:
                async with self._session.get(url, allow_redirects=True, max_redirects=5) as resp:
                    # Check content type — only process HTML
                    content_type = resp.headers.get("Content-Type", "")
                    if "text/html" not in content_type.lower():
                        logger.debug("Skipping non-HTML: %s (type=%s)", url, content_type)
                        return None

                    # Check content length if available
                    content_length = resp.headers.get("Content-Length")
                    if content_length and int(content_length) > self.max_response_size:
                        logger.debug("Skipping oversized page: %s (%s bytes)", url, content_length)
                        return None

                    # Read body with size limit
                    html = await resp.text(errors="replace")
                    if len(html) > self.max_response_size:
                        logger.debug("Truncated oversized response: %s", url)
                        html = html[: self.max_response_size]

                    self.total_fetched += 1

                    return {
                        "url": url,
                        "final_url": str(resp.url),
                        "status": resp.status,
                        "html": html,
                        "content_type": content_type,
                    }

            except asyncio.TimeoutError:
                logger.warning("Timeout fetching %s", url)
                self.total_errors += 1
                return None
            except aiohttp.ClientError as e:
                logger.warning("HTTP error fetching %s: %s", url, e)
                self.total_errors += 1
                return None
            except Exception as e:
                logger.warning("Unexpected error fetching %s: %s", url, e)
                self.total_errors += 1
                return None

    @property
    def is_rate_limited(self) -> bool:
        """Check if the rate limiter is currently throttling requests."""
        return self.rate_limiter.is_throttled

    @property
    def semaphore_available(self) -> int:
        """Number of available semaphore slots (approximate)."""
        # asyncio.Semaphore._value is not public API but useful for monitoring
        return getattr(self.semaphore, '_value', -1)
