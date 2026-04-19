# Architect Agent

## Role

System architect responsible for the overall design of the web crawler and search engine before any code is written. Produces the pipeline architecture, concurrency model, data schema, and component breakdown that all other agents build against.

## Responsibilities

- Define the multi-stage pipeline structure (stages, data flow between them)
- Choose the concurrency model (asyncio vs. multiprocessing vs. threading)
- Design the SQLite schema (tables, indexes, WAL configuration)
- Define backpressure strategy (where queues live, how they bound memory)
- Specify how concurrent search during active indexing is supported
- Produce the PRD that downstream agents use as their specification

## Prompt

> You are a software architect. Design a concurrent web crawler and search engine that runs on a single machine. It must support: BFS crawl up to depth k, deduplication, backpressure, TF-IDF search returning (url, origin_url, depth) triples, and real-time search updates during active crawling. Use Python stdlib as much as possible — no Scrapy, BeautifulSoup, Flask, or SQLAlchemy. Storage must be local. Output: pipeline stages, concurrency model, data schema, and component breakdown.

## Key Outputs

- Four-stage pipeline: Frontier Queue → Fetch → Parse → Index Writer
- Concurrency model: single asyncio event loop, N async fetch workers, thread pool for CPU-bound parse
- Backpressure: `asyncio.Queue(maxsize=N)` as the frontier; producers block when full
- Storage: SQLite WAL mode — concurrent reads (search) never block writes (indexing)
- Long-poll notification: `threading.Condition`, `notify_all()` after each batch commit
- Five-module breakdown: `db.py`, `fetcher.py`, `parser.py`, `engine.py`, `search.py`
- Full PRD (`product_prd.md`) with data schema, API spec, and acceptance criteria

## Decisions and Overrides

**Proposed:** Use `multiprocessing` for the parse stage to parallelize CPU work across cores.
**Decision:** Overridden. `asyncio.to_thread()` into a thread pool is sufficient — parse is not CPU-bound enough to justify IPC overhead. Revised to thread pool.

**Proposed:** Separate `asyncio.Queue` between parse and index writer (strict four-queue pipeline).
**Decision:** Simplified to an in-memory write buffer within the engine coroutine, flushed on batch size or timeout. Avoids an extra queue and simplifies shutdown coordination.

## Interfaces Produced

The Architect Agent's output is the contract all other agents build to:
- Pipeline stage boundaries and data shapes (what each stage receives and emits)
- Database schema (table definitions, indexes)
- API specification (endpoints, request/response shapes)
- Concurrency guarantees (which state is shared, how it is protected)
