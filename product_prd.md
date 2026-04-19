# Product Requirements Document (PRD)

Project: Build Google in a Day — Concurrent Web Crawler & Real-Time Search Engine

## 1. Document Control

Version: 2.1
Date: 2026-03-21
Status: Draft for implementation kickoff

## 2. Vision and Problem Statement

Build a minimal, production-minded, concurrent web crawler and real-time search engine using Python, prioritizing standard library usage. The system discovers pages recursively from a seed URL, indexes content in near real-time, and serves search results through a premium web dashboard with long-polling updates.

This project prioritizes:
1. Correctness under concurrency (no data races, no corruption)
2. Deterministic, thread-safe behavior across all shared state
3. Efficient persistence via SQLite WAL mode (stdlib `sqlite3`)
4. Asyncio-driven network I/O for maximum throughput with minimal memory
5. Simplicity and inspectability over framework abstraction
6. A visually impressive, production-quality UI

## 3. Scope

### In Scope
- Recursive, depth-bounded web crawler with `index(origin, k)` API
- Multi-stage pipeline: Frontier → Fetch → Parse → Index
- Asyncio-based concurrent fetch with backpressure controls
- SQLite WAL-backed persistence for pages, queue, tokens, and job metadata
- TF-IDF keyword search via `search(query)` returning `(url, origin_url, depth)` triples
- Long-polling API for near real-time search result updates
- Premium dark-theme web dashboard (Crawler, Status, Search pages)
- Resumability after interruption without re-crawling visited pages
- Observability: structured logs, queue depth, throughput metrics

### Out of Scope
- Distributed crawling across multiple machines
- JavaScript-rendered page execution (SPA content)
- Advanced ranking (PageRank, ML-based ranking, semantic embeddings)
- Full duplicate detection at internet scale (content-hash dedup)
- Full-text snippet semantic summarization

## 4. Non-Negotiable Constraints

1. Standard library first. All crawling, parsing, concurrency, indexing, and search logic must use Python stdlib. The only external dependency allowed is `aiohttp` for the async HTTP client and web server, justified because `urllib.request` is synchronous-only and `http.server` cannot handle async I/O efficiently. Every other component uses stdlib.
2. Thread-safe by design. All shared mutable state must be protected by explicit synchronization primitives. No read-modify-write on shared data without a lock.
3. Bounded resource usage. All queues bounded, all memory growth capped. The system must degrade gracefully under load, never crash from unbounded growth.
4. Crash-safe persistence. All writes to SQLite use transactions. The database must survive abrupt termination without corruption.

## 5. Allowed and Disallowed Libraries

### Allowed (Standard Library)
- `asyncio` — event loop, coroutines, async queues
- `sqlite3` — persistent storage (WAL mode)
- `html.parser` — HTML parsing and link/text extraction
- `urllib.parse` — URL normalization, joining, encoding
- `urllib.robotparser` — robots.txt parsing
- `threading` — condition variables for long-poll, thread pool
- `concurrent.futures` — ThreadPoolExecutor for blocking ops in async
- `queue` — thread-safe queue (for thread-pool stages if needed)
- `json` — API request/response serialization
- `re` — text tokenization, pattern matching
- `os`, `pathlib` — file system operations
- `time`, `logging` — utilities
- `signal` — graceful shutdown handling
- `hashlib` — content hashing for dedup
- `collections` — defaultdict, Counter for frequency counting
- `math` — TF-IDF calculations

### Allowed (External — Single Dependency)
- `aiohttp` — async HTTP client + web server. Justification: `urllib.request` is blocking; `http.server` is sync-only. `aiohttp` is the minimal async HTTP library that integrates with `asyncio`.

### Disallowed
Scrapy, BeautifulSoup (bs4), Selenium, Playwright, requests-html, pyppeteer, requests, httpx, SQLAlchemy, peewee, Flask, Django, FastAPI, NLTK, spaCy.

## 6. Primary Users and Use Cases

Users:
- Operator: developer running the crawler locally, configuring crawl parameters, monitoring progress.
- Searcher: end-user querying indexed pages via the web dashboard.

Core Use Cases:
1. Operator provides a seed URL and depth; crawler recursively discovers and indexes linked pages.
2. Operator monitors crawl progress in real-time via the Status dashboard (pages crawled, queue depth, backpressure state).
3. Operator pauses/resumes/stops a crawl job without losing progress.
4. Searcher submits a query; system returns ranked matching URLs as `(url, origin_url, depth)` triples.
5. Searcher's results update in near real-time as new pages are indexed (via long-polling).
6. System resists overload via bounded queues, rate limiting, and concurrency semaphores.
7. System resumes after crash/restart without re-crawling already-visited pages.

## 7. Functional Requirements

### FR-1: Recursive Crawler — `index(origin, k)`

Accept a seed URL (`origin`) and maximum depth (`k`). Crawl pages breadth-first, where depth is the number of hops from `origin`. Never crawl the same URL twice, even across restarts (persistent visited set in SQLite `pages` table). Parse links from HTML via `html.parser.HTMLParser` subclass.

URL normalization rules:
1. Resolve relative paths via `urllib.parse.urljoin`.
2. Remove fragment identifiers (`#section`).
3. Strip trailing slashes.
4. Canonicalize scheme and host to lowercase.
5. Filter to `http://` and `https://` schemes only (skip `mailto:`, `javascript:`, `tel:`, `ftp:`).

Additional rules:
- Respect `robots.txt` via `urllib.robotparser` (best-effort, cache per domain).
- Only process responses with `Content-Type: text/html` (skip images, PDFs, binary files).
- Configurable parameters per job: `max_depth`, `max_rate` (req/sec), `max_concurrent`, `max_queue_size`.

### FR-2: Multi-Stage Pipeline with Bounded Queues

The crawler operates as a four-stage producer-consumer pipeline:

Stage 1 — Frontier Queue: `asyncio.Queue(maxsize=N)` holds URLs to be fetched. Producers (link extractors) block when full. Each item contains: url, origin_url, depth, job_id.

Stage 2 — Fetch Stage: Async worker coroutines consume URLs from frontier. Each worker acquires a rate-limiter token and a concurrency semaphore slot before making the HTTP request via `aiohttp.ClientSession`. Returns: status_code, html_body, final_url, content_type.

Stage 3 — Parse Stage: Fetched HTML is parsed via `asyncio.to_thread()` to avoid blocking the event loop. Uses `html.parser.HTMLParser` subclass. Produces: title, visible body text, list of discovered links, token frequency map.

Stage 4 — Index Writer: Single-writer pattern. One dedicated async task collects parsed results, batches them (N pages or T seconds elapsed), and commits a single SQLite transaction. After commit, increments `index_version` and calls `condition.notify_all()` to wake long-poll clients.

Data flows between stages via asyncio queues. New links discovered in Stage 3 are filtered (not visited, depth ≤ k) and fed back into Stage 1.

### FR-3: Thread-Safe Indexing via SQLite WAL

Inverted index stored in `index_tokens` table: token maps to (url, origin_url, depth, tf_score, in_title). Document store in `pages` table: url maps to (origin_url, depth, title, body_text, word_count, crawled_at).

Key behaviors:
1. All writes use SQLite transactions — partial writes are impossible.
2. WAL mode enables concurrent reads (search) during writes (indexing) without blocking.
3. Index version is a monotonically increasing integer stored in `system_meta` table, incremented on each batch commit. Used by long-poll clients to detect new data.
4. Write batching: accumulate N parsed pages (default 50) or T seconds (default 5) before committing, amortizing disk I/O.
5. One write connection (serialized), separate read connections for search queries.

### FR-4: Search — `search(query)`

Accept a query string, return a list of `(url, origin_url, depth)` triples ranked by relevance.

Tokenization: lowercase, split on non-alphanumeric characters, filter tokens shorter than 2 characters, remove stop words (hard-coded list of ~150 common English stop words).

Scoring (TF-IDF with title boost):
- `TF(term, doc) = term_frequency_in_doc / total_words_in_doc`
- `IDF(term) = log(total_docs / docs_containing_term)`
- `score(doc) = sum of TF(term, doc) * IDF(term)` for each query term
- Title boost: 3x multiplier if term appears in the page title (`in_title = 1`).

Multi-term queries use implicit AND: all query terms must appear in a document for it to be a result. Return top-K results (default K=50), sorted by score descending.

Search must remain responsive during active crawling. Reads use a separate SQLite connection; WAL mode guarantees lock-free concurrent reads.

### FR-5: Long-Polling for Real-Time Updates

Client sends request with `last_version` parameter (the last index version it received). Server behavior:
- If `current_index_version > last_version`: immediately return fresh results.
- If `current_index_version == last_version`: block up to `timeout` seconds (default 30) until index is updated.

Implementation uses `threading.Condition`:

```python
# Long-poll handler pseudocode
def handle_updates(request):
    q = request.query['q']
    last_version = int(request.query['last_version'])
    timeout = int(request.query.get('timeout', 30))
    deadline = time.time() + timeout

    with index_condition:
        while get_index_version() <= last_version:
            remaining = deadline - time.time()
            if remaining <= 0:
                return {"updated": False, "index_version": last_version, "results": []}
            index_condition.wait(timeout=remaining)

    results = search(q)
    return {"updated": True, "index_version": get_index_version(), "results": results}
```

Indexer side: after committing a batch to SQLite, call `index_condition.notify_all()` to wake all waiting long-poll clients.

Safety guarantees:
1. `wait()` is always inside a `while` loop to guard against spurious wakeups.
2. Timeout prevents indefinite blocking.
3. Lock protects version read — no TOCTOU race.

Response payload format:
```json
{
  "updated": true,
  "index_version": 42,
  "results": [{"url": "...", "origin_url": "...", "depth": 2, "score": 0.85}]
}
```

### FR-6: Backpressure and Overload Protection

Three layers of backpressure:

Layer 1 — Rate Limiter: Token-bucket algorithm, default 10 req/sec. Fetch workers must acquire a token before each HTTP request. If bucket is empty, workers wait for refill. Capacity equals max_rate; refill rate is max_rate tokens/second.

Layer 2 — Concurrency Semaphore: `asyncio.Semaphore(max_concurrent)`, default 20 slots. Limits simultaneous in-flight HTTP requests. Excess fetch requests block until a slot frees.

Layer 3 — Queue Depth Limit: `asyncio.Queue(maxsize=max_queue)`, default 10,000 URLs. When full, link discovery (producers) blocks, preventing unbounded memory growth.

Additional protections:
- Per-host politeness delay: minimum 1 second between requests to the same domain.
- Maximum response body size: 5 MB cap, skip larger pages.
- Request timeout: 30 seconds per HTTP request.
- Adaptive throttling: if queue occupancy exceeds 80%, temporarily reduce frontier expansion rate.

Guarantees:
1. No unbounded memory growth under any workload.
2. Throughput degrades gracefully instead of crashing.
3. Backpressure state is visible via API and dashboard UI.

### FR-7: Persistence and Resumability

All state persisted in a single SQLite database file (`crawler.db`).

On interruption (SIGINT/SIGTERM):
1. Stop accepting new URLs into the frontier.
2. Wait for in-flight HTTP requests to complete (up to 10 second timeout).
3. Flush any buffered writes to SQLite.
4. Mark active jobs as `paused` in `crawl_jobs` table.
5. Pending queue items remain in `queue` table.

On restart:
1. Load `crawl_jobs` with status `paused` or `running`.
2. Reload pending URLs from `queue` table.
3. Skip any URL already in `pages` table (already crawled).
4. Resume crawling from where it left off.

Crash safety is guaranteed by SQLite WAL + transactions — no partial writes survive a crash.

## 8. Quality Attributes (Non-Functional Requirements)

### NFR-1: Thread Safety
- No data races in visited set, index map, or document metadata.
- SQLite handles its own internal locking (WAL mode for read/write concurrency).
- `threading.Condition` for long-poll notification: always used in a `while` loop for spurious wakeup safety.
- `asyncio.Queue` for pipeline stages: inherently safe within the event loop.
- Document lock ownership and critical sections in code comments where non-obvious.

### NFR-2: Performance Targets
- Crawl throughput (local network): 20–100 pages/minute (rate-limit dependent).
- Search p95 latency: less than 200ms for corpus of ~10k pages.
- Long-poll response latency: less than 500ms after index commit.
- Memory usage (10k page corpus): less than 500 MB.
- SQLite DB size (10k pages): less than 200 MB.

### NFR-3: Reliability
- Worker failures are isolated — one bad URL does not crash the entire crawl.
- HTTP errors (4xx, 5xx, timeouts, connection refused) are logged and skipped.
- Malformed HTML is handled gracefully (`html.parser` is lenient by default).
- SQLite transactions ensure atomicity — no half-written data.

### NFR-4: Observability
- Structured logs with timestamp, job_id, URL, depth, and outcome.
- Metrics exposed via API: active job count, pages crawled (per job and total), queue depth, crawl rate (pages/sec), backpressure indicators (rate limit active, queue full, semaphore exhausted), index version, error count.
- All metrics visible in the web dashboard Status page.

## 9. System Architecture

### 9.1 Component Overview

The system consists of six components:

1. Crawl Engine (`crawler/engine.py`): Orchestrates the BFS pipeline. Spawns async fetch workers, manages the frontier queue, enforces depth limits, and coordinates the four-stage pipeline.

2. Fetcher (`crawler/fetcher.py`): Async HTTP client using `aiohttp.ClientSession`. Implements token-bucket rate limiter and concurrency semaphore. Returns HTML content for valid pages.

3. Parser (`crawler/parser.py`): `html.parser.HTMLParser` subclass. Extracts links, title, and visible body text. Tokenizes text and computes term frequencies. Runs in a thread pool via `asyncio.to_thread()`.

4. Database Layer (`crawler/db.py`): SQLite with WAL mode. Manages all tables (pages, queue, index_tokens, crawl_jobs, system_meta). Provides CRUD operations and handles connection pooling.

5. Search Engine (`crawler/search.py`): Implements TF-IDF search on the `index_tokens` table. Operates on a separate read-only SQLite connection. Manages index versioning and the long-poll condition variable.

6. Web Server (`server/app.py`): `aiohttp`-based HTTP server. Serves REST API endpoints and static files (dashboard HTML/CSS/JS).

### 9.2 Pipeline Data Flow

The crawl pipeline processes URLs through four stages in sequence:

1. Seed URL is placed into the Frontier Queue (asyncio.Queue, bounded at maxsize) at depth 0.
2. N fetch worker coroutines (default 10) each loop: dequeue a URL from the frontier, acquire a rate-limiter token and semaphore slot, then fetch the page via aiohttp.
3. Fetched HTML is offloaded to a thread pool for parsing (asyncio.to_thread). The parser extracts: title text, visible body text (excluding script/style/noscript tags), all anchor href links (normalized), and a token→frequency map.
4. Parse results split into two paths: (a) newly discovered links are filtered (not visited, depth+1 ≤ k) and enqueued back into the frontier; (b) page content and tokens are sent to the index writer.
5. The index writer batches results and commits them to SQLite in a single transaction every N pages or T seconds.
6. After each commit, index_version increments and condition.notify_all() fires to wake long-poll clients.

### 9.3 Concurrency Model

- Event loop: single-threaded asyncio for all network I/O and coordination.
- Frontier queue: `asyncio.Queue(maxsize=N)` — bounded producer-consumer within the event loop.
- Fetch workers: N async coroutines with cooperative multitasking.
- Rate limiter: custom async token-bucket implementation. Tokens refill at configurable rate.
- Concurrency cap: `asyncio.Semaphore(M)` limits max in-flight HTTP requests.
- HTML parsing: offloaded to `concurrent.futures.ThreadPoolExecutor` via `asyncio.to_thread()` to avoid blocking the event loop.
- SQLite writes: single-writer async task. All writes serialized through one task to eliminate lock contention.
- SQLite reads (search): separate connection using WAL mode for lock-free concurrent reads.
- Long-poll notification: `threading.Condition` — `wait(timeout)` for clients, `notify_all()` from indexer.
- Graceful shutdown: `asyncio.Event` set by `signal` handlers. All workers check this event and exit cooperatively.

## 10. Data Model

### 10.1 Database Schema

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

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
```

### 10.2 Core Entity Descriptions

Page entity fields: url (TEXT, primary key, canonical URL), origin_url (TEXT, seed URL passed to index()), depth (INT, hops from origin where 0 is origin itself), title (TEXT, extracted title tag), body_text (TEXT, visible text content for snippets), word_count (INT, total token count for TF normalization), content_hash (TEXT, SHA-256 of body for content dedup), crawled_at (REAL, epoch timestamp).

Token posting fields: token (TEXT, lowercased normalized word), url (TEXT, URL of containing page), origin_url (TEXT, seed URL for this crawl), depth (INT, depth at which page was found), tf (REAL, term frequency = count(token) / word_count), in_title (INT, 1 if token appears in page title).

Index version: stored in system_meta table as key='index_version'. Monotonically increasing integer, incremented on each batch commit. Used by long-poll clients to detect new data.

## 11. API Specification

### 11.1 Crawl Management

POST /api/crawl — Start a new crawl job. Request body: `{"origin": "url", "depth": N, "max_rate": 10, "max_concurrent": 20, "max_queue": 10000}`. Response: `{"job_id": "...", "status": "running"}`.

GET /api/jobs — List all crawl jobs with their current status, pages_crawled, urls_queued, and timestamps.

GET /api/jobs/{id} — Get detailed status of a specific job including all metrics.

POST /api/jobs/{id}/pause — Pause a running job. Response: `{"status": "paused"}`.

POST /api/jobs/{id}/resume — Resume a paused job. Response: `{"status": "running"}`.

POST /api/jobs/{id}/stop — Stop and finalize a job. Response: `{"status": "completed"}`.

### 11.2 Search

GET /api/search?q={query}&k={topK} — Search indexed pages. Default k=50. Response: `{"results": [{"url", "origin_url", "depth", "score"}], "total": N, "index_version": V}`.

### 11.3 Long-Polling Updates

GET /api/updates?q={query}&k={topK}&last_version={V}&timeout={sec} — Long-poll for new search results. Server blocks until index_version advances past last_version or timeout is reached. Default timeout=30. Response: `{"updated": bool, "index_version": V, "results": [...]}`.

### 11.4 System Status

GET /api/status — Global system status. Response: `{"active_jobs": N, "total_pages": N, "index_version": V, "jobs": [...]}`.

## 12. Web Dashboard Specification

### 12.1 Layout

Single-page application with three tab views: Crawler, Status, Search.

### 12.2 Crawler Page

Input form with: URL input field (required), depth input field (required, integer >= 1), expandable "Advanced Settings" section with max rate slider (default 10), max concurrent slider (default 20), max queue size input (default 10000). Start Crawl button calls POST /api/crawl.

Below the form: job history table showing all previous jobs with job ID, origin URL, status badge (Running/Completed/Paused/Failed), pages crawled, and created time. Clicking a job navigates to the Status page for that job.

### 12.3 Status Page

Job selector dropdown or auto-selected from Crawler page link. Metrics displayed: pages crawled (animated counter), queue depth (color-coded: green below 50%, yellow 50-80%, red above 80%), crawl rate in pages/sec, elapsed time, error count, backpressure indicators (rate limit active, queue full, semaphore exhausted). Control buttons: Pause, Resume, Stop. Live activity log showing last 20 events with auto-scroll. Polls `/api/jobs/{id}` every 1 second.

### 12.4 Search Page

Search input with 300ms debounce after last keystroke. Each result shows: page title (clickable link), URL, origin URL and depth, relevance score. Long-poll integration: after initial results load, client issues long-poll to `/api/updates`; when new results arrive, they animate into the list. "Feeling Lucky" button for random search. Result count and index version displayed.

### 12.5 Design Requirements

- Dark theme with glassmorphism card effects (semi-transparent backgrounds with backdrop blur).
- Google Fonts: Inter for typography.
- Smooth micro-animations on hover, state changes, and data updates.
- Responsive layout for desktop and tablet.
- Color palette: background #0a0a0f, cards rgba(255,255,255,0.05) with blur, accent #6366f1 (indigo), success #10b981, warning #f59e0b, error #ef4444.
- No external CSS frameworks — vanilla CSS only.

## 13. Configuration Defaults

- max_depth: required, max hops from origin.
- max_rate: 10, requests per second (token bucket).
- max_concurrent: 20, max simultaneous HTTP requests.
- max_queue: 10000, max URLs in frontier queue.
- worker_count: 10, async fetch worker tasks.
- batch_size: 50, pages per index write transaction.
- batch_timeout: 5, seconds before forcing a commit even if batch_size not reached.
- request_timeout: 30, HTTP request timeout in seconds.
- max_response_size: 5242880 (5 MB), skip pages larger than this.
- politeness_delay: 1.0, minimum seconds between requests to same domain.
- server_port: 8080, web dashboard port.
- long_poll_timeout: 30, default long-poll wait in seconds.
- user_agent: "GoogleInOneDay-Crawler/2.0", HTTP User-Agent header.

## 14. Security and Safety

1. Validate URL schemes: only http:// and https:// allowed.
2. Enforce max_response_size to prevent memory abuse from huge pages.
3. Timeout all network operations (connection + read).
4. Sanitize page titles and snippets rendered in UI to prevent XSS.
5. SQLite database is local-only; no authentication needed for localhost service.
6. Respect robots.txt (best-effort) to be a good web citizen.

## 15. Project File Structure

Root directory: `GoogleInOneDay-Crawler2/`

crawler/ package:
- `__init__.py` — package init
- `db.py` — SQLite schema, WAL config, all CRUD operations, connection management
- `fetcher.py` — async HTTP client with token-bucket rate limiter and concurrency semaphore
- `parser.py` — html.parser.HTMLParser subclass for link extraction, text extraction, tokenization
- `engine.py` — BFS crawl pipeline orchestrator implementing index(origin, k)
- `search.py` — TF-IDF search engine implementing search(query), index versioning, long-poll condition

server/ package:
- `__init__.py` — package init
- `app.py` — aiohttp web server with all API routes and static file serving

static/ directory:
- `index.html` — premium dark-theme dashboard (single-page app with tab navigation)
- `style.css` — glassmorphism CSS design system
- `app.js` — live polling, long-poll client, search logic, UI interactions

Root files:
- `main.py` — CLI entry point with subcommands: serve, crawl, search
- `requirements.txt` — single dependency: aiohttp
- `product_prd.md` — this file
- `recommendation.md` — production deployment recommendations
- `README.md` — setup and usage documentation

## 16. Testing Strategy

### Unit Tests
- URL normalization and deduplication (edge cases with fragments, trailing slashes, relative paths).
- HTML parsing: link extraction accuracy, text extraction excluding script/style, encoding handling.
- Tokenizer: lowercasing, stop word removal, frequency counting correctness.
- TF-IDF scoring: known-answer tests with a fixed small corpus.
- Token-bucket rate limiter: timing and capacity tests.

### Concurrency Tests
- High-contention SQLite writes with many concurrent workers.
- Long-poll notify/wait correctness: no missed notifications, no spurious wakeup bugs.
- Queue saturation: verify producers block when queue is full.
- Graceful shutdown under load: verify no data loss.

### Integration Tests
- Crawl a live test site, verify all expected pages appear in database.
- Search returns correct results with expected ranking order.
- Long-poll delivers updates within 500ms of index commit.
- Restart recovery: interrupt a crawl, restart the process, verify it resumes without re-crawling visited pages.

## 17. Implementation Phases

Phase 1 (hour 0-1): Project skeleton, db.py (schema + CRUD), requirements.txt.
Phase 2 (hour 1-2): fetcher.py (async client + rate limiter + semaphore), parser.py (HTML parser + tokenizer).
Phase 3 (hour 2-3): engine.py (BFS pipeline, workers, backpressure, graceful shutdown).
Phase 4 (hour 3-4): search.py (TF-IDF scoring, index versioning, long-poll condition variable).
Phase 5 (hour 4-5): app.py (API routes, long-poll handler, static file serving).
Phase 6 (hour 5-6): index.html, style.css, app.js (premium dashboard UI).
Phase 7 (hour 6-7): main.py CLI, README.md, recommendation.md.
Phase 8 (hour 7-8): error handling hardening, edge cases, graceful shutdown testing, resumability verification.

## 18. Acceptance Criteria

1. Given a seed URL and depth k, the crawler discovers and indexes all reachable pages within k hops without crawling any page twice.
2. Under concurrent load, no data corruption, race conditions, or crashes occur.
3. search(query) returns relevant (url, origin_url, depth) triples ranked by TF-IDF score during active crawling.
4. Long-poll endpoint delivers updated search results within 500ms of new pages being indexed.
5. Queue saturation triggers visible backpressure and keeps memory bounded.
6. Index and queue survive interruption; crawl resumes on restart with no re-crawling.
7. Web dashboard provides real-time visibility into crawl progress, queue depth, and backpressure state.
8. Search page shows results updating live as new content is indexed.

## 19. Risks and Mitigations

- Risk: SQLite write contention slows indexing. Mitigation: single-writer pattern + batch commits (50 pages per transaction).
- Risk: Crawl traps or infinite URL spaces. Mitigation: URL normalization, max depth, max pages per job, query parameter filtering.
- Risk: Long-poll thread exhaustion. Mitigation: bounded server worker threads; timeout on all waits.
- Risk: Target server rate-limiting or blocking. Mitigation: per-host politeness delay, respectful User-Agent, robots.txt compliance.
- Risk: Memory growth from large pages. Mitigation: max response size (5 MB), streaming response reads.
- Risk: Malformed HTML crashes parser. Mitigation: html.parser feeds errors are caught; try/except around all parsing.

## 20. Definition of Done

1. All PRD functional requirements implemented.
2. All API endpoints operational and returning correct data.
3. Web dashboard renders and updates in real-time.
4. Long-poll delivers live search updates.
5. Backpressure controls visibly function under load.
6. Crawl resumes after interruption without data loss.
7. README.md documents setup, usage, and architecture.
8. recommendation.md documents production deployment guidance.
9. Live demo: crawl a real site + search during crawling + see real-time results.
