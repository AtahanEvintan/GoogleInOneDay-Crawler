# UI Agent

## Role

Full-stack engineer responsible for the aiohttp web server, all REST API routes, and the frontend web dashboard. Owns the user-facing interface for initiating crawls, monitoring job progress, and searching indexed content in real time.

## Responsibilities

- Implement `server/app.py`: aiohttp application with all API routes and static file serving
- Implement `static/index.html`, `static/style.css`, `static/app.js`: single-page dashboard with three tabs
- API routes: POST /api/crawl, GET /api/jobs, GET /api/jobs/{id}, POST /api/jobs/{id}/pause|resume|stop, GET /api/search, GET /api/updates (long-poll), GET /api/status, GET /api/random-word
- Dashboard: Crawler tab (start job, job history), Status tab (live metrics, log stream, controls), Search tab (search input, live results, long-poll updates)
- Dark glassmorphism theme — no external CSS frameworks, vanilla JS only

## Prompt

> You are a full-stack engineer. Implement `server/app.py` (aiohttp web server) and `static/index.html`, `static/style.css`, `static/app.js` (single-page dashboard). API routes needed: POST /api/crawl, GET /api/jobs, GET /api/jobs/{id}, POST /api/jobs/{id}/pause|resume|stop, GET /api/search, GET /api/updates (long-poll), GET /api/status. Dashboard: dark theme, three tabs (Crawler / Status / Search), live job metrics with 1-second polling, live log stream per job, search with 300ms debounce and long-poll result updates, no external CSS frameworks, no React or Vue — vanilla JS only.

## Key Outputs

- `server/app.py`: aiohttp `Application` with route registration, CORS headers, JSON response helpers, and static file serving from `./static/`
- Long-poll route runs `search.search_with_long_poll()` via `asyncio.to_thread()` to avoid blocking the event loop
- `static/index.html`: single HTML file with three tab panels, job history table, metrics cards, log terminal, search results list
- `static/style.css`: glassmorphism design — `rgba(255,255,255,0.05)` cards with `backdrop-filter: blur`, indigo accent `#6366f1`, dark background `#0a0a0f`
- `static/app.js`: tab router, `setInterval` polling for job stats (1s), 300ms debounce on search input, long-poll loop that re-issues request after each response, animated counter updates
- Queue depth indicator: CSS class swap (`green`/`yellow`/`red`) based on `urls_queued / max_queue` ratio
- Job history table on Crawler tab (added beyond original spec — kept as it improves usability)
- Backpressure badges: "Rate Limited", "Queue Full", "Semaphore Exhausted" displayed on Status tab

## Decisions and Overrides

**Proposed:** Run `search.wait_for_update()` directly in the aiohttp route handler.
**Decision:** Revised. `threading.Condition.wait()` is blocking — calling it directly in an async handler would block the event loop. Moved to `asyncio.to_thread()` so it runs in the thread pool.

**Proposed:** Use WebSockets for real-time dashboard updates (job metrics and search results).
**Decision:** Declined. Long-polling for search updates and `setInterval` polling for job stats are sufficient and simpler. WebSockets would require protocol upgrade handling and persistent connection management.

**Proposed:** Add a fourth tab for "Index Explorer" showing all indexed tokens and their frequencies.
**Decision:** Declined. Out of scope for the current PRD. The Search tab covers the query use case.

**Accepted addition:** Job history table on the Crawler tab (not in original spec). Keeps the user oriented across sessions without navigating to the Status tab.

## Interfaces Consumed

- `CrawlEngine` from `crawler/engine.py`: `start_crawl()`, `get_stats()`, `get_all_stats()`, `pause_job()`, `resume_job()`, `stop_job()`, `shutdown()`
- `SearchEngine` from `crawler/search.py`: `search()`, `search_with_long_poll()`
- `Database` from `crawler/db.py`: `get_all_jobs()`, `index_version`

## Interfaces Produced

- REST API (consumed by frontend and external clients)
- Static dashboard (consumed by human operators)
- `create_app(db, engine, search) -> aiohttp.Application`
