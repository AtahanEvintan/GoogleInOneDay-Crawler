# Multi-Agent Workflow

## Overview

This project was developed using a multi-agent AI workflow. Five specialized agents were defined, each responsible for a distinct domain of the system. Each agent was given a focused prompt and context, produced an independent proposal or implementation, and I (the human) reviewed their outputs, resolved conflicts, and made final architectural decisions.

The agents did not communicate with each other directly. I acted as the coordinator — taking output from one agent, evaluating it, and passing relevant context into the next agent's prompt when needed.

---

## Agents Defined

| Agent | Responsibility |
|---|---|
| Architect Agent | System design, data model, pipeline architecture |
| Crawler Agent | BFS engine, backpressure, rate limiting, fetch workers |
| Indexer Agent | HTML parsing, tokenization, SQLite schema, batch writes |
| Search Agent | TF-IDF scoring, long-poll, index versioning |
| UI Agent | Web dashboard, API routes, frontend interactions |

---

## Agent 1: Architect Agent

**Role:** Design the overall system before any code is written. Define the pipeline stages, concurrency model, storage schema, and constraints.

**Prompt given:**
> You are a software architect. Design a concurrent web crawler and search engine that runs on a single machine. It must support: BFS crawl up to depth k, deduplication, backpressure, TF-IDF search returning (url, origin_url, depth) triples, and real-time search updates during active crawling. Use Python stdlib as much as possible — no Scrapy, BeautifulSoup, Flask, or SQLAlchemy. Storage must be local. Output: pipeline stages, concurrency model, data schema, and component breakdown.

**Output summary:**
The Architect Agent proposed a four-stage pipeline: Frontier Queue → Fetch → Parse → Index Writer. It recommended `asyncio` as the concurrency primitive, `asyncio.Queue` with `maxsize` for backpressure, SQLite WAL mode for concurrent reads during writes, and a `threading.Condition` for long-poll notification. It proposed five modules: `db.py`, `fetcher.py`, `parser.py`, `engine.py`, `search.py`.

**Key decision point:**
The agent initially proposed using `multiprocessing` for the parse stage to parallelize CPU work. I overrode this — the parse load in practice is light enough that `asyncio.to_thread()` into a thread pool is sufficient and avoids IPC complexity. The agent accepted the constraint and updated its design.

**Output used:** Full pipeline architecture, concurrency model, and database schema fed directly into the PRD and into the prompts for all subsequent agents.

---

## Agent 2: Crawler Agent

**Role:** Implement the BFS crawl engine — the frontier queue, fetch workers, rate limiting, concurrency semaphore, and backpressure logic.

**Prompt given:**
> You are a Python async systems engineer. Implement `crawler/engine.py` and `crawler/fetcher.py` for a web crawler. Requirements: BFS with max depth k, asyncio.Queue frontier (bounded for backpressure), token-bucket rate limiter (configurable req/sec), asyncio.Semaphore for max concurrent HTTP requests, N async fetch workers, graceful shutdown via asyncio.Event on SIGINT/SIGTERM, persist crawl state to SQLite on pause, resume from persisted state on restart. Use aiohttp for HTTP. No external libraries beyond aiohttp. The fetcher should expose: is_rate_limited (bool), semaphore_available (int) for observability.

**Output summary:**
The Crawler Agent produced the `CrawlEngine` class with `start_crawl()`, `pause_job()`, `resume_job()`, and `shutdown()`. It implemented a token-bucket rate limiter in `Fetcher` using `asyncio.sleep` and a refill loop. Backpressure is surfaced via `stats.rate_limit_active`, `stats.queue_full`, and `stats.semaphore_slots`.

**Key decision point:**
The agent proposed a separate `asyncio.Queue` stage between parse and index writer (a true four-queue pipeline). I simplified this — the index writer is just a buffer within the same coroutine scope, flushed on batch size or timeout. This avoids an extra queue and simplifies shutdown coordination without changing the observable behavior.

**Key decision point:**
The agent wanted to skip URLs that exceeded the depth limit silently. I asked it to also dequeue and discard them from the frontier cleanly so queue depth stats stay accurate. It revised accordingly.

**Output used:** `crawler/engine.py` and `crawler/fetcher.py` in full.

---

## Agent 3: Indexer Agent

**Role:** Implement HTML parsing, text extraction, tokenization, and the SQLite database layer including the inverted index schema and batch write logic.

**Prompt given:**
> You are a Python engineer specializing in text processing and databases. Implement `crawler/parser.py` and `crawler/db.py`. Parser requirements: use html.parser.HTMLParser subclass, extract visible body text (exclude script/style/noscript), extract page title, extract and normalize anchor hrefs (resolve relative paths, strip fragments, filter to http/https). Tokenizer: lowercase, split on non-alphanumeric, remove stop words, compute term frequencies. DB requirements: SQLite WAL mode, tables for pages, queue, index_tokens, crawl_jobs, system_meta. Batch insert for index_tokens. Monotonically incrementing index_version in system_meta. threading.Condition for long-poll notification on each batch commit. No ORM — raw sqlite3 only.

**Output summary:**
The Indexer Agent produced a clean `HTMLParser` subclass that tracks tag depth to suppress script/style content. The tokenizer uses a hard-coded stop word set of ~150 English words. The `Database` class uses WAL mode, a single write connection, and separate read connections for search. Batch inserts use `executemany` with `INSERT OR REPLACE`. The `index_condition` threading.Condition is stored on the `Database` object.

**Key decision point:**
The agent initially used `INSERT OR IGNORE` for duplicate tokens, which would silently drop re-crawled pages. I asked it to use `INSERT OR REPLACE` so re-crawled pages update their TF scores. The agent flagged that this adds write amplification — I accepted the tradeoff given the deduplication logic already prevents most re-crawls.

**Key decision point:**
The agent proposed a separate `FTS5` virtual table for full-text search as an alternative to the manual inverted index. I declined — FTS5 is an external extension in some SQLite builds and the manual TF-IDF approach keeps the project stdlib-compliant and transparent.

**Output used:** `crawler/parser.py` and `crawler/db.py` in full.

---

## Agent 4: Search Agent

**Role:** Implement TF-IDF search, index versioning, and the long-poll mechanism for real-time result updates.

**Prompt given:**
> You are a search engineer. Implement `crawler/search.py`. Requirements: search(query) tokenizes query using the same tokenizer as the indexer, looks up index_tokens table for matching tokens (AND semantics — all tokens must be present), scores results using TF-IDF with 3x title boost, returns top-K results as list of {url, origin_url, depth, score, title}. Must run on a separate read-only SQLite connection (WAL mode guarantees no blocking). Long-poll: wait_for_update(last_version, timeout) blocks on threading.Condition until index_version advances. search_with_long_poll() combines both. No external search libraries.

**Output summary:**
The Search Agent implemented TF-IDF scoring in pure Python with `math.log`. It uses per-token IDF lookup against the `pages` table for document count, then joins `index_tokens` for per-doc TF scores. The long-poll correctly uses a `while` loop around `condition.wait()` to handle spurious wakeups. It also added a `get_random_word()` helper for a "Feeling Lucky" feature.

**Key decision point:**
The agent proposed caching IDF scores in memory and refreshing them periodically. I rejected this — with SQLite WAL, a fresh IDF query per search is fast enough at the scale of this project, and a cache introduces staleness complexity. The agent noted this would not scale past ~100k documents; I accepted that tradeoff as out-of-scope per the PRD.

**Output used:** `crawler/search.py` in full.

---

## Agent 5: UI Agent

**Role:** Implement the web server API routes and the frontend dashboard (HTML/CSS/JS).

**Prompt given:**
> You are a full-stack engineer. Implement `server/app.py` (aiohttp web server) and `static/index.html`, `static/style.css`, `static/app.js` (single-page dashboard). API routes needed: POST /api/crawl, GET /api/jobs, GET /api/jobs/{id}, POST /api/jobs/{id}/pause|resume|stop, GET /api/search, GET /api/updates (long-poll), GET /api/status. Dashboard: dark theme, three tabs (Crawler / Status / Search), live job metrics with 1-second polling, live log stream per job, search with 300ms debounce and long-poll result updates, no external CSS frameworks, no React or Vue — vanilla JS only.

**Output summary:**
The UI Agent produced all three frontend files plus the aiohttp server. The dashboard uses glassmorphism card effects, animated metric counters, color-coded queue depth indicators (green/yellow/red), and an auto-scrolling log panel. The search page issues a long-poll after each result load and animates new results in. The agent added a job history table on the Crawler tab not originally specified — I kept it as it adds clear value.

**Key decision point:**
The agent initially ran the long-poll handler synchronously inside an aiohttp route handler, which would block the event loop. I asked it to run `search.wait_for_update()` in a thread via `asyncio.to_thread()` since `threading.Condition.wait()` is a blocking call. The agent revised this correctly.

**Key decision point:**
The agent proposed WebSockets for real-time updates instead of long-polling. I kept long-polling to stay closer to the PRD specification and avoid adding a WebSocket upgrade path to the server.

**Output used:** `server/app.py`, `static/index.html`, `static/style.css`, `static/app.js` in full with the above revision.

---

## Integration and Coordination

After all five agents delivered their outputs, I integrated the components:

1. **Interface alignment:** The Crawler Agent's `CrawlStats` object exposed different field names than what the UI Agent's API routes expected. I reconciled these by updating `to_dict()` in `CrawlStats` to match the API contract the UI Agent had assumed.

2. **Shutdown coordination:** The Crawler Agent's shutdown logic flushed the write buffer before exiting, but the Indexer Agent's batch flush method was async while the shutdown path was partially sync. I made all flush calls consistently async with `await`.

3. **CLI integration:** I wrote `main.py` myself as the final integration layer, wiring together the DB, engine, search, and server with proper signal handling and argument parsing.

4. **Resumability verification:** I ran the full system, interrupted it mid-crawl, and verified that the queue table preserved pending URLs and the engine correctly reloaded them on restart without re-crawling visited pages.

---

## Decisions I Made as Coordinator

| Decision | Rationale |
|---|---|
| Rejected multiprocessing for parse stage | Thread pool sufficient; avoids IPC complexity |
| Rejected FTS5 virtual table | Keeps stdlib compliance; TF-IDF is transparent |
| Rejected IDF caching in memory | Staleness complexity outweighs performance gain at this scale |
| Rejected WebSockets | Long-poll sufficient per PRD; simpler server |
| Kept job history table (UI Agent addition) | Clear usability value, no downside |
| Single write connection for SQLite | Eliminates lock contention; batch commits amortize the cost |

---

## Thoughts on Search During Active Indexing

The system already supports concurrent search during indexing via SQLite WAL mode — readers never block writers. The long-poll mechanism (`threading.Condition.notify_all()` after each batch commit) means search clients receive updated results within seconds of new pages being indexed.

For a more advanced design where search must run while a very large-scale indexer is active, the following approaches would help:

1. **Separate read replica:** Write to one SQLite file, periodically `VACUUM INTO` a read-only copy for search. Search reads from the copy; no contention with writes.

2. **Event-driven invalidation:** Instead of polling, the indexer publishes a domain event (via Redis Pub/Sub or an in-process queue) after each batch. The search layer subscribes and refreshes its cached result sets on demand.

3. **Snapshot isolation:** Each search request reads from a consistent snapshot version (SQLite's `BEGIN DEFERRED` gives this for free). Long-running searches see a stable index even as new pages are being committed concurrently.

4. **Dedicated search process:** Move the search engine to a separate process with its own read connection. The indexer process signals the search process via a Unix socket or named pipe after each batch. This eliminates any GIL contention between the asyncio crawler and the search thread pool.
