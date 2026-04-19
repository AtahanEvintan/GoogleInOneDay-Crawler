"""
SQLite database layer with WAL mode for concurrent read/write safety.

Manages all persistent state: crawled pages, BFS queue, inverted index tokens,
crawl job metadata, and system metadata (index version).

Thread-safety: SQLite WAL mode allows concurrent readers during writes.
One write connection (serialized), separate read connections for search.
All writes wrapped in transactions for atomicity.
"""

import sqlite3
import time
import threading
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Default database path
DEFAULT_DB_PATH = "crawler.db"

# Schema SQL — executed once on init
_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS pages (
    url           TEXT PRIMARY KEY,
    origin_url    TEXT NOT NULL,
    depth         INTEGER NOT NULL,
    title         TEXT DEFAULT '',
    body_text     TEXT DEFAULT '',
    word_count    INTEGER DEFAULT 0,
    content_hash  TEXT DEFAULT '',
    crawled_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS queue (
    url           TEXT NOT NULL,
    origin_url    TEXT NOT NULL,
    depth         INTEGER NOT NULL,
    job_id        TEXT NOT NULL,
    status        TEXT DEFAULT 'pending',
    created_at    REAL NOT NULL,
    PRIMARY KEY (url, job_id)
);

CREATE TABLE IF NOT EXISTS index_tokens (
    token         TEXT NOT NULL,
    url           TEXT NOT NULL,
    origin_url    TEXT NOT NULL,
    depth         INTEGER NOT NULL,
    tf            REAL NOT NULL,
    in_title      INTEGER DEFAULT 0,
    PRIMARY KEY (token, url)
);

CREATE TABLE IF NOT EXISTS crawl_jobs (
    job_id        TEXT PRIMARY KEY,
    origin_url    TEXT NOT NULL,
    max_depth     INTEGER NOT NULL,
    status        TEXT DEFAULT 'running',
    pages_crawled INTEGER DEFAULT 0,
    urls_discovered INTEGER DEFAULT 0,
    urls_queued   INTEGER DEFAULT 0,
    errors        INTEGER DEFAULT 0,
    max_rate      REAL DEFAULT 10.0,
    max_concurrent INTEGER DEFAULT 20,
    max_queue     INTEGER DEFAULT 10000,
    elapsed_seconds REAL DEFAULT 0.0,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS system_meta (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tokens_token ON index_tokens(token);
CREATE INDEX IF NOT EXISTS idx_queue_job_status ON queue(job_id, status);
CREATE INDEX IF NOT EXISTS idx_pages_origin ON pages(origin_url);
"""


class Database:
    """
    SQLite database manager with WAL mode for concurrent read/write.

    Usage:
        db = Database("crawler.db")
        db.init()
        # ... use db methods ...
        db.close()

    Concurrency model:
        - One write connection used by the crawl engine (serialized writes).
        - Separate read connections can be created for search (WAL allows concurrent reads).
        - index_version + threading.Condition for long-poll notification.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._write_conn: sqlite3.Connection | None = None
        self._write_lock = threading.Lock()

        # Index version for long-poll support
        self._index_version = 0
        self._index_condition = threading.Condition()

    def init(self):
        """Initialize the database: create tables, set WAL mode, load index version."""
        self._write_conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._write_conn.row_factory = sqlite3.Row

        # Execute schema (PRAGMA + CREATE TABLE statements)
        for statement in _SCHEMA_SQL.strip().split(";"):
            statement = statement.strip()
            if statement:
                self._write_conn.execute(statement)
        self._write_conn.commit()

        # Auto-migrate elapsed_seconds for existing databases
        try:
            self._write_conn.execute("ALTER TABLE crawl_jobs ADD COLUMN elapsed_seconds REAL DEFAULT 0.0")
            self._write_conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Load or initialize index_version
        row = self._write_conn.execute(
            "SELECT value FROM system_meta WHERE key = 'index_version'"
        ).fetchone()
        if row:
            self._index_version = int(row["value"])
        else:
            self._write_conn.execute(
                "INSERT INTO system_meta (key, value) VALUES ('index_version', '0')"
            )
            self._write_conn.commit()
            self._index_version = 0

        logger.info(
            "Database initialized at %s (index_version=%d)",
            self.db_path,
            self._index_version,
        )

    def close(self):
        """Close the write connection."""
        if self._write_conn:
            self._write_conn.close()
            self._write_conn = None

    def get_read_connection(self) -> sqlite3.Connection:
        """
        Create a new read-only connection for search queries.
        WAL mode allows this to read concurrently while the write connection writes.
        Caller is responsible for closing the returned connection.
        """
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # ── Index Version (Long-Poll Support) ────────────────────────────

    @property
    def index_version(self) -> int:
        return self._index_version

    @property
    def index_condition(self) -> threading.Condition:
        return self._index_condition

    def increment_index_version(self):
        """
        Increment the index version and notify all long-poll waiters.
        Called by the index writer after committing a batch.
        """
        with self._write_lock:
            self._index_version += 1
            self._write_conn.execute(
                "UPDATE system_meta SET value = ? WHERE key = 'index_version'",
                (str(self._index_version),),
            )
            self._write_conn.commit()

        # Notify long-poll clients outside the write lock
        with self._index_condition:
            self._index_condition.notify_all()

        logger.debug("Index version incremented to %d", self._index_version)

    # ── Pages (Visited URLs) ─────────────────────────────────────────

    def is_visited(self, url: str) -> bool:
        """Check if a URL has already been crawled."""
        with self._write_lock:
            row = self._write_conn.execute(
                "SELECT 1 FROM pages WHERE url = ?", (url,)
            ).fetchone()
        return row is not None

    def bulk_check_visited(self, urls: list[str]) -> set[str]:
        """Return the subset of urls that have already been visited."""
        if not urls:
            return set()
        with self._write_lock:
            placeholders = ",".join("?" for _ in urls)
            rows = self._write_conn.execute(
                f"SELECT url FROM pages WHERE url IN ({placeholders})", urls
            ).fetchall()
        return {row["url"] for row in rows}

    def insert_page(
        self,
        url: str,
        origin_url: str,
        depth: int,
        title: str = "",
        body_text: str = "",
        word_count: int = 0,
        content_hash: str = "",
    ):
        """Insert a crawled page record."""
        with self._write_lock:
            self._write_conn.execute(
                """INSERT OR IGNORE INTO pages
                   (url, origin_url, depth, title, body_text, word_count, content_hash, crawled_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (url, origin_url, depth, title, body_text, word_count, content_hash, time.time()),
            )

    def insert_pages_batch(self, pages: list[dict]):
        """
        Insert multiple pages and their tokens in a single transaction.
        Each page dict should contain: url, origin_url, depth, title, body_text,
        word_count, content_hash, tokens (dict of token -> (tf, in_title)).
        """
        now = time.time()
        with self._write_lock:
            cursor = self._write_conn.cursor()
            try:
                for page in pages:
                    cursor.execute(
                        """INSERT OR IGNORE INTO pages
                           (url, origin_url, depth, title, body_text, word_count, content_hash, crawled_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            page["url"],
                            page["origin_url"],
                            page["depth"],
                            page.get("title", ""),
                            page.get("body_text", ""),
                            page.get("word_count", 0),
                            page.get("content_hash", ""),
                            now,
                        ),
                    )

                    # Insert tokens for this page
                    tokens = page.get("tokens", {})
                    for token, (tf, in_title) in tokens.items():
                        cursor.execute(
                            """INSERT OR REPLACE INTO index_tokens
                               (token, url, origin_url, depth, tf, in_title)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (
                                token,
                                page["url"],
                                page["origin_url"],
                                page["depth"],
                                tf,
                                1 if in_title else 0,
                            ),
                        )

                self._write_conn.commit()
            except Exception:
                self._write_conn.rollback()
                raise

        logger.info("Batch committed: %d pages", len(pages))

    def get_total_pages(self) -> int:
        """Get the total number of crawled pages."""
        with self._write_lock:
            row = self._write_conn.execute("SELECT COUNT(*) as cnt FROM pages").fetchone()
        return row["cnt"] if row else 0

    def get_total_tokens(self) -> int:
        """Get the total number of indexed tokens."""
        with self._write_lock:
            row = self._write_conn.execute(
                "SELECT COUNT(DISTINCT token) as cnt FROM index_tokens"
            ).fetchone()
        return row["cnt"] if row else 0

    # ── Queue (BFS Frontier Persistence) ─────────────────────────────

    def enqueue_urls(self, urls: list[dict]):
        """
        Persist URLs to the queue table for resumability.
        Each dict should have: url, origin_url, depth, job_id.
        """
        now = time.time()
        with self._write_lock:
            for item in urls:
                self._write_conn.execute(
                    """INSERT OR IGNORE INTO queue
                       (url, origin_url, depth, job_id, status, created_at)
                       VALUES (?, ?, ?, ?, 'pending', ?)""",
                    (item["url"], item["origin_url"], item["depth"], item["job_id"], now),
                )
            self._write_conn.commit()

    def dequeue_pending(self, job_id: str, limit: int = 100) -> list[dict]:
        """
        Fetch pending URLs from the queue for a given job.
        Marks them as 'processing' atomically.
        """
        with self._write_lock:
            rows = self._write_conn.execute(
                """SELECT url, origin_url, depth FROM queue
                   WHERE job_id = ? AND status = 'pending'
                   LIMIT ?""",
                (job_id, limit),
            ).fetchall()

            if rows:
                urls = [row["url"] for row in rows]
                placeholders = ",".join("?" for _ in urls)
                self._write_conn.execute(
                    f"""UPDATE queue SET status = 'processing'
                        WHERE job_id = ? AND url IN ({placeholders})""",
                    [job_id] + urls,
                )
                self._write_conn.commit()

        return [dict(row) for row in rows]

    def mark_queue_done(self, job_id: str, url: str):
        """Mark a queue item as done after successful crawl."""
        with self._write_lock:
            self._write_conn.execute(
                "UPDATE queue SET status = 'done' WHERE job_id = ? AND url = ?",
                (job_id, url),
            )
            self._write_conn.commit()

    def get_pending_count(self, job_id: str) -> int:
        """Get count of pending URLs in queue for a job."""
        with self._write_lock:
            row = self._write_conn.execute(
                "SELECT COUNT(*) as cnt FROM queue WHERE job_id = ? AND status = 'pending'",
                (job_id,),
            ).fetchone()
        return row["cnt"] if row else 0

    # ── Crawl Jobs ───────────────────────────────────────────────────

    def create_job(
        self,
        job_id: str,
        origin_url: str,
        max_depth: int,
        max_rate: float = 10.0,
        max_concurrent: int = 20,
        max_queue: int = 10000,
    ) -> dict:
        """Create a new crawl job record."""
        now = time.time()
        with self._write_lock:
            self._write_conn.execute(
                """INSERT INTO crawl_jobs
                   (job_id, origin_url, max_depth, status, max_rate, max_concurrent,
                    max_queue, created_at, updated_at, elapsed_seconds)
                   VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)""",
                (job_id, origin_url, max_depth, max_rate, max_concurrent, max_queue, now, now, 0.0),
            )
            self._write_conn.commit()

        logger.info("Created job %s: origin=%s depth=%d", job_id, origin_url, max_depth)
        return {
            "job_id": job_id,
            "origin_url": origin_url,
            "max_depth": max_depth,
            "status": "running",
            "pages_crawled": 0,
            "urls_discovered": 0,
            "urls_queued": 0,
            "errors": 0,
            "max_rate": max_rate,
            "max_concurrent": max_concurrent,
            "max_queue": max_queue,
            "created_at": now,
            "updated_at": now,
        }

    def update_job_stats(
        self,
        job_id: str,
        pages_crawled: int | None = None,
        urls_discovered: int | None = None,
        urls_queued: int | None = None,
        errors: int | None = None,
        status: str | None = None,
        elapsed_seconds: float | None = None,
    ):
        """Update crawl job statistics. Only updates non-None fields."""
        updates = []
        params = []

        if pages_crawled is not None:
            updates.append("pages_crawled = ?")
            params.append(pages_crawled)
        if urls_discovered is not None:
            updates.append("urls_discovered = ?")
            params.append(urls_discovered)
        if urls_queued is not None:
            updates.append("urls_queued = ?")
            params.append(urls_queued)
        if errors is not None:
            updates.append("errors = ?")
            params.append(errors)
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if elapsed_seconds is not None:
            updates.append("elapsed_seconds = ?")
            params.append(elapsed_seconds)

        if not updates:
            return

        updates.append("updated_at = ?")
        params.append(time.time())
        params.append(job_id)

        with self._write_lock:
            self._write_conn.execute(
                f"UPDATE crawl_jobs SET {', '.join(updates)} WHERE job_id = ?",
                params,
            )
            self._write_conn.commit()

    def get_job(self, job_id: str) -> dict | None:
        """Get a single crawl job by ID."""
        with self._write_lock:
            row = self._write_conn.execute(
                "SELECT * FROM crawl_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_all_jobs(self) -> list[dict]:
        """Get all crawl jobs, ordered by creation time descending."""
        with self._write_lock:
            rows = self._write_conn.execute(
                "SELECT * FROM crawl_jobs ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_active_jobs(self) -> list[dict]:
        """Get all jobs with status 'running' or 'paused'."""
        with self._write_lock:
            rows = self._write_conn.execute(
                "SELECT * FROM crawl_jobs WHERE status IN ('running', 'paused') ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def set_job_status(self, job_id: str, status: str):
        """Set job status (running, paused, completed, failed)."""
        self.update_job_stats(job_id, status=status)

    # ── Search Queries (run on read connection) ──────────────────────

    def search_tokens(
        self,
        tokens: list[str],
        read_conn: sqlite3.Connection,
        limit: int = 50,
    ) -> list[dict]:
        """
        Search the inverted index for pages matching all given tokens.
        Uses the specific quiz scoring formula:
        score = (frequency * 10) + 1000 - (depth * 5)
        """
        if not tokens:
            return []

        active_tokens = tokens
        placeholders = ",".join("?" for _ in active_tokens)
        
        rows = read_conn.execute(
            f"""SELECT it.url, it.origin_url, it.depth, it.token, it.tf, it.in_title,
                       p.title, p.word_count
                FROM index_tokens it
                JOIN pages p ON p.url = it.url
                WHERE it.token IN ({placeholders})""",
            active_tokens,
        ).fetchall()

        # Group by URL, accumulate scores
        url_tokens: dict[str, set] = {}
        url_data: dict[str, dict] = {}
        
        for row in rows:
            url = row["url"]
            token = row["token"]

            if url not in url_tokens:
                url_tokens[url] = set()
                url_data[url] = {
                    "url": url,
                    "origin_url": row["origin_url"],
                    "depth": row["depth"],
                    "score": 0.0,
                    "title": row["title"] or "",
                }

            url_tokens[url].add(token)
            
            # Quiz Formula calculation
            freq = round(row["tf"] * row["word_count"])
            depth = row["depth"]
            score = (freq * 10) + 1000 - (depth * 5)
            
            url_data[url]["score"] += score

        # Filter to URLs that have ALL tokens (AND semantics)
        required = set(active_tokens)
        results = {
            url: data
            for url, data in url_data.items()
            if url_tokens[url] >= required
        }

        # Sort by score descending, limit results
        sorted_results = sorted(results.values(), key=lambda x: x["score"], reverse=True)
        return sorted_results[:limit]
