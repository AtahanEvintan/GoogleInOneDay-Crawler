"""
BFS crawl engine implementing index(origin, k).

Orchestrates the multi-stage pipeline:
  Frontier Queue → Fetch Workers → Parse (thread pool) → Index Writer

Backpressure: bounded asyncio.Queue, rate limiter, concurrency semaphore.
Resumability: persists queue state to SQLite on shutdown.
Graceful shutdown: SIGINT/SIGTERM sets shutdown event, workers drain cooperatively.
"""

import asyncio
import time
import logging
import os
from collections import deque
from dataclasses import dataclass, field

from crawler.db import Database
from crawler.fetcher import Fetcher
from crawler.parser import parse_html, compute_tokens

logger = logging.getLogger(__name__)

# Defaults
DEFAULT_WORKER_COUNT = 10
DEFAULT_BATCH_SIZE = 50
DEFAULT_BATCH_TIMEOUT = 5.0  # seconds


@dataclass
class CrawlStats:
    """Live statistics for a running crawl job."""

    job_id: str
    origin_url: str
    max_depth: int
    status: str = "running"
    pages_crawled: int = 0
    urls_discovered: int = 0
    urls_queued: int = 0
    errors: int = 0
    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    previous_elapsed: float = 0.0
    rate_limit_active: bool = False
    queue_full: bool = False
    semaphore_slots: int = -1
    recent_logs: deque = field(default_factory=lambda: deque(maxlen=200))

    def log(self, msg: str):
        timestamp = time.strftime("%H:%M:%S")
        log_line = f"[{timestamp}] {msg}"
        self.recent_logs.append(log_line)
        
        # Write to dedicated log file
        try:
            with open(f"logs/{self.job_id}.log", "a", encoding="utf-8") as f:
                f.write(log_line + "\n")
        except Exception as e:
            logger.error(f"Failed to write to job log: {e}")

    @property
    def elapsed(self) -> float:
        if self.end_time is not None:
            return self.previous_elapsed + (self.end_time - self.start_time)
        return self.previous_elapsed + (time.time() - self.start_time)

    @property
    def pages_per_second(self) -> float:
        elapsed = self.elapsed
        if elapsed <= 0:
            return 0.0
        return self.pages_crawled / elapsed

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "origin_url": self.origin_url,
            "max_depth": self.max_depth,
            "status": self.status,
            "pages_crawled": self.pages_crawled,
            "urls_discovered": self.urls_discovered,
            "urls_queued": self.urls_queued,
            "errors": self.errors,
            "elapsed_seconds": round(self.elapsed, 1),
            "pages_per_second": round(self.pages_per_second, 2),
            "rate_limit_active": self.rate_limit_active,
            "queue_full": self.queue_full,
            "semaphore_slots": self.semaphore_slots,
            "logs": list(self.recent_logs),
        }


class CrawlEngine:
    """
    Manages one or more concurrent crawl jobs.

    Usage:
        engine = CrawlEngine(db)
        job_id = await engine.start_crawl("https://example.com", depth=3)
        stats = engine.get_stats(job_id)
        await engine.shutdown()
    """

    def __init__(self, db: Database):
        self.db = db
        self._jobs: dict[str, CrawlStats] = {}
        self._job_tasks: dict[str, asyncio.Task] = {}
        self._job_events: dict[str, asyncio.Event] = {}  # stop events
        self._fetchers: dict[str, Fetcher] = {}
        self._shutdown_event = asyncio.Event()
        
        # Ensure logs directory exists
        os.makedirs("logs", exist_ok=True)

        # Recover zombie jobs from previous crash (mark dangling "running" as "paused")
        try:
            active_jobs = self.db.get_active_jobs()
            for job in active_jobs:
                if job["status"] == "running":
                    self.db.set_job_status(job["job_id"], "paused")
                    logger.info("Marked dangling job %s as paused.", job["job_id"])
        except Exception as e:
            logger.error("Failed to recover dangling jobs: %s", e)


    async def start_crawl(
        self,
        origin: str,
        depth: int,
        max_rate: float = 10.0,
        max_concurrent: int = 20,
        max_queue: int = 10000,
        worker_count: int = DEFAULT_WORKER_COUNT,
        batch_size: int = DEFAULT_BATCH_SIZE,
        job_id: str | None = None,
    ) -> str:
        """
        Start a new crawl job or resume an existing one. Returns the job_id.

        Creates a background asyncio task that runs the BFS pipeline.
        """
        is_resume = job_id is not None
        job_id = job_id or str(int(time.time() * 1000))

        if not is_resume:
            # Persist job to DB
            self.db.create_job(
                job_id=job_id,
                origin_url=origin,
                max_depth=depth,
                max_rate=max_rate,
                max_concurrent=max_concurrent,
                max_queue=max_queue,
            )
        else:
            await asyncio.to_thread(self.db.set_job_status, job_id, "running")

        # Create stats tracker
        stats = CrawlStats(
            job_id=job_id,
            origin_url=origin,
            max_depth=depth,
        )
        if is_resume:
            job_data = await asyncio.to_thread(self.db.get_job, job_id)
            if job_data:
                stats.pages_crawled = job_data.get("pages_crawled", 0)
                stats.urls_discovered = job_data.get("urls_discovered", 0)
                stats.urls_queued = job_data.get("urls_queued", 0)
                stats.errors = job_data.get("errors", 0)
                stats.previous_elapsed = job_data.get("elapsed_seconds", 0.0)
        self._jobs[job_id] = stats

        # Stop event for this job
        stop_event = asyncio.Event()
        self._job_events[job_id] = stop_event

        # Start the crawl task
        task = asyncio.create_task(
            self._run_crawl(
                job_id=job_id,
                origin=origin,
                max_depth=depth,
                max_rate=max_rate,
                max_concurrent=max_concurrent,
                max_queue=max_queue,
                worker_count=worker_count,
                batch_size=batch_size,
                stop_event=stop_event,
                stats=stats,
                is_resume=is_resume,
            )
        )
        self._job_tasks[job_id] = task

        action_str = "Resumed" if is_resume else "Started"
        stats.log(f"{action_str} crawl job {job_id} for origin {origin} (depth={depth})")
        logger.info("%s crawl job %s: origin=%s depth=%d", action_str, job_id, origin, depth)
        return job_id

    async def _run_crawl(
        self,
        job_id: str,
        origin: str,
        max_depth: int,
        max_rate: float,
        max_concurrent: int,
        max_queue: int,
        worker_count: int,
        batch_size: int,
        stop_event: asyncio.Event,
        stats: CrawlStats,
        is_resume: bool = False,
    ):
        """Run the BFS crawl pipeline for a single job."""

        # Frontier queue — bounded for backpressure
        frontier: asyncio.Queue = asyncio.Queue(maxsize=max_queue)

        # Index write buffer — batched for efficiency
        write_buffer: list[dict] = []
        buffer_lock = asyncio.Lock()
        last_flush_time = time.time()

        # Track active workers for clean shutdown
        active_workers = 0
        workers_done = asyncio.Event()

        # Create fetcher for this job
        fetcher = Fetcher(
            max_rate=max_rate,
            max_concurrent=max_concurrent,
        )
        self._fetchers[job_id] = fetcher

        async with fetcher:
            # For a new job, always seed the frontier with the origin URL
            # even if globally visited, so we at least fetch it once for this job.
            if not is_resume:
                await frontier.put((origin, origin, 0))
                stats.urls_queued += 1
                await asyncio.to_thread(
                    self.db.enqueue_urls,
                    [{"url": origin, "origin_url": origin, "depth": 0, "job_id": job_id}]
                )
            else:
                stats.log(f"🔄 Resuming from previously persisted queue state...")

            # Load any pending URLs from a previous interrupted/paused run
            # Also reset any 'processing' URLs back to 'pending' from a dirty stop
            await asyncio.to_thread(
                self.db._write_conn.execute,
                "UPDATE queue SET status = 'pending' WHERE job_id = ? AND status = 'processing'",
                (job_id,)
            )
            
            pending = await asyncio.to_thread(self.db.dequeue_pending, job_id, limit=max_queue)
            for item in pending:
                if not frontier.full():
                    await frontier.put(
                        (item["url"], item["origin_url"], item["depth"])
                    )
                    stats.urls_queued += 1

            async def flush_buffer():
                """Flush the write buffer to SQLite and increment index version."""
                nonlocal write_buffer, last_flush_time
                async with buffer_lock:
                    if not write_buffer:
                        return
                    batch = write_buffer[:]
                    write_buffer = []

                # Run DB write in thread to avoid blocking event loop
                await asyncio.to_thread(self.db.insert_pages_batch, batch)
                await asyncio.to_thread(self.db.increment_index_version)

                # Update job stats in DB
                await asyncio.to_thread(
                    self.db.update_job_stats,
                    job_id,
                    pages_crawled=stats.pages_crawled,
                    urls_discovered=stats.urls_discovered,
                    urls_queued=stats.urls_queued,
                    errors=stats.errors,
                )
                last_flush_time = time.time()

            async def worker(worker_id: int):
                """Fetch worker: dequeue URL → fetch → parse → buffer for index."""
                nonlocal active_workers
                active_workers += 1

                try:
                    while not stop_event.is_set() and not self._shutdown_event.is_set():
                        try:
                            url, origin_url, depth = await asyncio.wait_for(
                                frontier.get(), timeout=2.0
                            )
                        except asyncio.TimeoutError:
                            # Check if we should exit (no more work)
                            if frontier.empty():
                                break
                            continue

                        # Skip if already globally visited (unless it's the root origin of a new crawl)
                        is_visited = await asyncio.to_thread(self.db.is_visited, url)
                        if is_visited and not (depth == 0 and url == origin):
                            frontier.task_done()
                            stats.urls_queued = max(0, stats.urls_queued - 1)
                            continue

                        # Skip if over max depth
                        if depth > max_depth:
                            frontier.task_done()
                            stats.urls_queued = max(0, stats.urls_queued - 1)
                            continue

                        # Mark as visited if it was pending queue locally (handled automatically by pages insert)

                        # Fetch the page
                        result = await fetcher.fetch(url)

                        if result is None or result["status"] != 200:
                            stats.errors += 1
                            err_msg = f"HTTP {result['status'] if result else 'Err'} on {url}"
                            stats.log(f"❌ {err_msg}")
                            frontier.task_done()
                            stats.urls_queued = max(0, stats.urls_queued - 1)
                            continue

                        # Parse HTML in thread pool (CPU work)
                        parse_result = await asyncio.to_thread(
                            parse_html, result["html"], result["final_url"]
                        )

                        # Compute tokens for indexing
                        tokens, word_count = await asyncio.to_thread(
                            compute_tokens,
                            parse_result["body_text"],
                            parse_result["title"],
                        )

                        # Buffer the page for batch write
                        page_data = {
                            "url": result["final_url"],
                            "origin_url": origin_url,
                            "depth": depth,
                            "title": parse_result["title"],
                            "body_text": parse_result["body_text"][:10000],  # Truncate for storage
                            "word_count": word_count,
                            "content_hash": parse_result["content_hash"],
                            "tokens": tokens,
                        }

                        async with buffer_lock:
                            write_buffer.append(page_data)

                        stats.pages_crawled += 1
                        stats.urls_queued = max(0, stats.urls_queued - 1)

                        # Update backpressure indicators
                        stats.rate_limit_active = fetcher.is_rate_limited
                        stats.semaphore_slots = fetcher.semaphore_available
                        stats.queue_full = frontier.full()

                        # Discover new links at depth+1
                        if depth + 1 <= max_depth:
                            new_urls = parse_result["links"]
                            # Bulk check visited status
                            visited = await asyncio.to_thread(
                                self.db.bulk_check_visited, new_urls
                            )

                            to_enqueue = []
                            for link_url in new_urls:
                                if link_url not in visited:
                                    stats.urls_discovered += 1
                                    try:
                                        frontier.put_nowait(
                                            (link_url, origin_url, depth + 1)
                                        )
                                        stats.urls_queued += 1
                                        to_enqueue.append({
                                            "url": link_url,
                                            "origin_url": origin_url,
                                            "depth": depth + 1,
                                            "job_id": job_id
                                        })
                                    except asyncio.QueueFull:
                                        # Backpressure: queue is full, skip this link
                                        stats.queue_full = True
                                        break
                            
                            if to_enqueue:
                                await asyncio.to_thread(self.db.enqueue_urls, to_enqueue)

                        # Flush buffer if batch size reached or timeout
                        should_flush = False
                        async with buffer_lock:
                            should_flush = (
                                len(write_buffer) >= batch_size
                                or (time.time() - last_flush_time) > DEFAULT_BATCH_TIMEOUT
                            )
                        if should_flush:
                            await flush_buffer()
                            stats.log(f"💾 Committed batch to database")

                        # Mark url as successfully processed in queue
                        await asyncio.to_thread(self.db.mark_queue_done, job_id, url)
                        frontier.task_done()

                        stats.log(f"✅ Crawled {url} (depth={depth}, found {len(parse_result['links'])} links)")

                        logger.debug(
                            "[Job %s][W%d] Crawled %s (depth=%d, links=%d)",
                            job_id,
                            worker_id,
                            url,
                            depth,
                            len(parse_result["links"]),
                        )

                except asyncio.CancelledError:
                    stats.log(f"⚠️ Worker {worker_id} cancelled")
                    logger.info("[Job %s][W%d] Worker cancelled", job_id, worker_id)
                except Exception as e:
                    stats.log(f"⚠️ Worker {worker_id} exception: {str(e)}")
                    logger.error("[Job %s][W%d] Worker error: %s", job_id, worker_id, e)
                    stats.errors += 1
                finally:
                    active_workers -= 1
                    if active_workers == 0:
                        workers_done.set()

            # Spawn worker tasks
            worker_tasks = [
                asyncio.create_task(worker(i)) for i in range(worker_count)
            ]

            # Wait for all workers to finish
            await asyncio.gather(*worker_tasks, return_exceptions=True)

            # Final flush of any remaining buffered pages
            await flush_buffer()

        # Update final status
        final_status = "completed"
        if stop_event.is_set():
            final_status = "paused"
        elif self._shutdown_event.is_set():
            final_status = "paused"

        stats.status = final_status
        stats.end_time = time.time()
        stats.log(f"🛑 Job ended with status: {final_status}")
        await asyncio.to_thread(
            self.db.update_job_stats,
            job_id,
            pages_crawled=stats.pages_crawled,
            urls_discovered=stats.urls_discovered,
            urls_queued=stats.urls_queued,
            errors=stats.errors,
            status=final_status,
            elapsed_seconds=stats.elapsed,
        )

        # Clean up
        self._fetchers.pop(job_id, None)

        logger.info(
            "Job %s %s: %d pages crawled, %d errors",
            job_id,
            final_status,
            stats.pages_crawled,
            stats.errors,
        )

    def get_stats(self, job_id: str) -> dict | None:
        """Get live stats for a job (from memory if running, else from DB)."""
        if job_id in self._jobs:
            return self._jobs[job_id].to_dict()

        # Job not in memory — load from DB
        job = self.db.get_job(job_id)
        if job:
            return {
                "job_id": job["job_id"],
                "origin_url": job["origin_url"],
                "max_depth": job["max_depth"],
                "status": job["status"],
                "pages_crawled": job["pages_crawled"],
                "urls_discovered": job["urls_discovered"],
                "urls_queued": job["urls_queued"],
                "errors": job["errors"],
                "elapsed_seconds": job.get("elapsed_seconds", 0.0),
                "pages_per_second": 0.0,
                "rate_limit_active": False,
                "queue_full": False,
                "semaphore_slots": -1,
                "logs": [f"ℹ️ Loaded historical job {job_id} from database (logs not available)."],
            }
        return None

    def get_all_stats(self) -> list[dict]:
        """Get stats for all jobs (live + completed)."""
        all_jobs = self.db.get_all_jobs()
        results = []
        for job in all_jobs:
            jid = job["job_id"]
            if jid in self._jobs:
                results.append(self._jobs[jid].to_dict())
            else:
                results.append({
                    "job_id": jid,
                    "origin_url": job["origin_url"],
                    "max_depth": job["max_depth"],
                    "status": job["status"],
                    "pages_crawled": job["pages_crawled"],
                    "urls_discovered": job["urls_discovered"],
                    "urls_queued": job["urls_queued"],
                    "errors": job["errors"],
                    "elapsed_seconds": job.get("elapsed_seconds", 0.0),
                    "pages_per_second": 0.0,
                    "rate_limit_active": False,
                    "queue_full": False,
                    "semaphore_slots": -1,
                    "created_at": job["created_at"],
                    "logs": [],
                })
        return results

    async def pause_job(self, job_id: str) -> bool:
        """Pause a running crawl job."""
        if job_id in self._job_events:
            self._job_events[job_id].set()
            if job_id in self._jobs:
                self._jobs[job_id].status = "paused"
            return True
        return False

    async def resume_job(self, job_id: str) -> str | None:
        """Resume a paused crawl job. Returns job_id or None if can't resume."""
        job = self.db.get_job(job_id)
        if not job or job["status"] not in ("paused", "running"):
            return None

        # Start the crawl machinery using the SAME parameters and job_id
        await self.start_crawl(
            origin=job["origin_url"],
            depth=job["max_depth"],
            max_rate=job["max_rate"],
            max_concurrent=job["max_concurrent"],
            max_queue=job["max_queue"],
            job_id=job_id,
        )
        return job_id

    async def stop_job(self, job_id: str) -> bool:
        """Stop a running crawl job permanently."""
        if job_id in self._job_events:
            self._job_events[job_id].set()
            if job_id in self._jobs:
                self._jobs[job_id].status = "completed"
            await asyncio.to_thread(self.db.set_job_status, job_id, "completed")
            return True
        return False

    async def shutdown(self):
        """Gracefully shutdown all running crawl jobs."""
        logger.info("Shutting down crawl engine...")
        self._shutdown_event.set()

        # Signal all jobs to stop
        for event in self._job_events.values():
            event.set()

        # Wait for all job tasks to complete (with timeout)
        if self._job_tasks:
            tasks = list(self._job_tasks.values())
            await asyncio.gather(*tasks, return_exceptions=True)

        logger.info("Crawl engine shutdown complete")
