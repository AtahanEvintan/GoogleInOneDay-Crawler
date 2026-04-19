# Crawler Agent

## Role

Systems engineer responsible for the BFS crawl engine and async HTTP fetcher. Owns the frontier queue, fetch worker pool, rate limiting, concurrency control, backpressure indicators, and job lifecycle (start, pause, resume, stop, shutdown).

## Responsibilities

- Implement `crawler/engine.py`: `CrawlEngine` class with `start_crawl()`, `_run_crawl()`, `pause_job()`, `resume_job()`, `stop_job()`, `shutdown()`
- Implement `crawler/fetcher.py`: `Fetcher` class with token-bucket rate limiter, `asyncio.Semaphore` concurrency cap, per-host politeness delay
- BFS frontier as `asyncio.Queue(maxsize=max_queue)` — blocks producers when full
- N async fetch worker coroutines with cooperative shutdown via `asyncio.Event`
- Graceful SIGINT/SIGTERM handling: drain in-flight requests, flush write buffer, persist queue state
- Surface backpressure state: `rate_limit_active`, `semaphore_available`, `queue_full`
- Resumability: persist pending URLs to SQLite `queue` table; reload on resume

## Prompt

> You are a Python async systems engineer. Implement `crawler/engine.py` and `crawler/fetcher.py` for a web crawler. Requirements: BFS with max depth k, asyncio.Queue frontier (bounded for backpressure), token-bucket rate limiter (configurable req/sec), asyncio.Semaphore for max concurrent HTTP requests, N async fetch workers, graceful shutdown via asyncio.Event on SIGINT/SIGTERM, persist crawl state to SQLite on pause, resume from persisted state on restart. Use aiohttp for HTTP. No external libraries beyond aiohttp. The fetcher should expose: is_rate_limited (bool), semaphore_available (int) for observability.

## Key Outputs

- `CrawlEngine`: manages multiple concurrent crawl jobs, each as an independent asyncio Task
- `CrawlStats`: live per-job metrics dataclass with `to_dict()` for API serialization
- Token-bucket rate limiter: async refill loop, workers await token acquisition before each fetch
- Bounded frontier queue: `asyncio.QueueFull` caught on `put_nowait` — sets `queue_full` flag and skips link rather than crashing
- Batch write buffer: accumulate N pages or T seconds, then flush to DB in one transaction
- Per-job log ring buffer (`deque(maxlen=200)`) mirrored to `logs/{job_id}.log`
- Crash recovery: on startup, dangling `running` jobs are marked `paused`

## Decisions and Overrides

**Proposed:** Strict four-queue pipeline with a separate asyncio.Queue between parse and index writer.
**Decision:** Simplified to an in-memory write buffer. Cleaner shutdown path, no extra coordination.

**Proposed:** Silently skip URLs exceeding max depth without dequeuing.
**Decision:** Revised to explicitly dequeue and decrement `urls_queued` counter so stats remain accurate.

**Proposed:** Use `asyncio.wait_for` with a short timeout on frontier dequeue as the idle check.
**Decision:** Kept — 2-second timeout per worker is a reasonable polling interval without busy-waiting.

## Interfaces Consumed

- `Database` from `crawler/db.py`: `is_visited()`, `bulk_check_visited()`, `enqueue_urls()`, `dequeue_pending()`, `insert_pages_batch()`, `increment_index_version()`, `mark_queue_done()`
- `parse_html()`, `compute_tokens()` from `crawler/parser.py` (via `asyncio.to_thread`)

## Interfaces Produced

- `CrawlEngine.start_crawl(origin, depth, ...) -> job_id`
- `CrawlEngine.get_stats(job_id) -> dict`
- `CrawlEngine.get_all_stats() -> list[dict]`
- `CrawlEngine.pause_job(job_id)`, `resume_job(job_id)`, `stop_job(job_id)`, `shutdown()`
