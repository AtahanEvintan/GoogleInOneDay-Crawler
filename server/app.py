"""
aiohttp web server serving REST API endpoints and static files.

API Routes:
  POST /api/crawl           — Start a new crawl job
  GET  /api/jobs            — List all crawl jobs
  GET  /api/jobs/{id}       — Get specific job status
  POST /api/jobs/{id}/pause — Pause a running job
  POST /api/jobs/{id}/resume — Resume a paused job
  POST /api/jobs/{id}/stop  — Stop a job
  GET  /api/status          — Global system status
  GET  /api/search          — Search indexed pages
  GET  /search              — Quiz-format search (scoring formula)
  GET  /api/updates         — Long-poll for new search results
  GET  /api/random-word     — Random indexed word (Feeling Lucky)
  GET  /                    — Serve dashboard HTML
  GET  /static/*            — Serve static assets
"""

import asyncio
import json
import logging
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

# Path to static files
STATIC_DIR = Path(__file__).parent.parent / "static"


def create_app(db, engine, search_engine) -> web.Application:
    """
    Create the aiohttp web application with all routes.

    Args:
        db: Database instance
        engine: CrawlEngine instance
        search_engine: SearchEngine instance
    """
    app = web.Application()

    # Store references in app for handler access
    app["db"] = db
    app["engine"] = engine
    app["search"] = search_engine

    # Register routes
    app.router.add_post("/api/crawl", handle_crawl)
    app.router.add_get("/api/jobs", handle_list_jobs)
    app.router.add_get("/api/jobs/{job_id}", handle_job_status)
    app.router.add_post("/api/jobs/{job_id}/pause", handle_pause_job)
    app.router.add_post("/api/jobs/{job_id}/resume", handle_resume_job)
    app.router.add_post("/api/jobs/{job_id}/stop", handle_stop_job)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/search", handle_search)
    app.router.add_get("/search", handle_quiz_search)
    app.router.add_get("/api/updates", handle_updates)
    app.router.add_get("/api/random-word", handle_random_word)

    # Static file serving
    app.router.add_get("/", handle_index)
    app.router.add_static("/static/", path=str(STATIC_DIR), name="static")

    # Middleware for CORS
    app.middlewares.append(cors_middleware)

    return app


@web.middleware
async def cors_middleware(request, handler):
    """Add CORS headers to all responses."""
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


# ── Static File Handlers ─────────────────────────────────────────

async def handle_index(request):
    """Serve the main dashboard HTML."""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return web.FileResponse(index_path)
    return web.Response(text="Dashboard not found", status=404)


# ── Crawl Management Handlers ────────────────────────────────────

async def handle_crawl(request):
    """
    POST /api/crawl
    Start a new crawl job.

    Body: {"origin": "url", "depth": N, "max_rate": 10, "max_concurrent": 20, "max_queue": 10000}
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    origin = body.get("origin", "").strip()
    depth = body.get("depth")

    if not origin:
        return web.json_response({"error": "origin URL is required"}, status=400)
    if depth is None or not isinstance(depth, int) or depth < 1:
        return web.json_response({"error": "depth must be a positive integer"}, status=400)

    # Optional parameters with defaults
    max_rate = float(body.get("max_rate", 10.0))
    max_concurrent = int(body.get("max_concurrent", 20))
    max_queue = int(body.get("max_queue", 10000))

    engine = request.app["engine"]
    job_id = await engine.start_crawl(
        origin=origin,
        depth=depth,
        max_rate=max_rate,
        max_concurrent=max_concurrent,
        max_queue=max_queue,
    )

    return web.json_response({
        "job_id": job_id,
        "status": "running",
        "origin": origin,
        "depth": depth,
    })


async def handle_list_jobs(request):
    """GET /api/jobs — List all crawl jobs."""
    engine = request.app["engine"]
    jobs = engine.get_all_stats()
    return web.json_response({"jobs": jobs})


async def handle_job_status(request):
    """GET /api/jobs/{job_id} — Get specific job status."""
    job_id = request.match_info["job_id"]
    engine = request.app["engine"]
    stats = engine.get_stats(job_id)

    if stats is None:
        return web.json_response({"error": "Job not found"}, status=404)

    return web.json_response(stats)


async def handle_pause_job(request):
    """POST /api/jobs/{job_id}/pause — Pause a running job."""
    job_id = request.match_info["job_id"]
    engine = request.app["engine"]
    success = await engine.pause_job(job_id)

    if success:
        return web.json_response({"job_id": job_id, "status": "paused"})
    return web.json_response({"error": "Job not found or not running"}, status=404)


async def handle_resume_job(request):
    """POST /api/jobs/{job_id}/resume — Resume a paused job."""
    job_id = request.match_info["job_id"]
    engine = request.app["engine"]
    new_job_id = await engine.resume_job(job_id)

    if new_job_id:
        return web.json_response({"job_id": new_job_id, "status": "running"})
    return web.json_response({"error": "Job not found or cannot resume"}, status=404)


async def handle_stop_job(request):
    """POST /api/jobs/{job_id}/stop — Stop a job permanently."""
    job_id = request.match_info["job_id"]
    engine = request.app["engine"]
    success = await engine.stop_job(job_id)

    if success:
        return web.json_response({"job_id": job_id, "status": "completed"})
    return web.json_response({"error": "Job not found or not running"}, status=404)


# ── System Status ────────────────────────────────────────────────

async def handle_status(request):
    """GET /api/status — Global system status."""
    db = request.app["db"]
    engine = request.app["engine"]

    jobs = engine.get_all_stats()
    active_jobs = [j for j in jobs if j.get("status") == "running"]
    total_pages = await asyncio.to_thread(db.get_total_pages)
    total_tokens = await asyncio.to_thread(db.get_total_tokens)

    return web.json_response({
        "active_jobs": len(active_jobs),
        "total_jobs": len(jobs),
        "total_pages": total_pages,
        "total_tokens": total_tokens,
        "index_version": db.index_version,
        "jobs": jobs,
    })


# ── Search Handlers ──────────────────────────────────────────────

async def handle_search(request):
    """
    GET /api/search?q={query}&k={limit}
    Search indexed pages.
    """
    query = request.query.get("q", "").strip()
    limit = int(request.query.get("k", "50"))

    if not query:
        return web.json_response({"error": "query parameter 'q' is required"}, status=400)

    search_engine = request.app["search"]
    result = await asyncio.to_thread(search_engine.search, query, limit)

    return web.json_response(result)


async def handle_updates(request):
    """
    GET /api/updates?q={query}&last_version={V}&k={limit}&timeout={sec}
    Long-poll for new search results.
    """
    query = request.query.get("q", "").strip()
    last_version = int(request.query.get("last_version", "0"))
    limit = int(request.query.get("k", "50"))
    timeout = min(float(request.query.get("timeout", "30")), 60.0)

    if not query:
        return web.json_response({"error": "query parameter 'q' is required"}, status=400)

    search_engine = request.app["search"]

    # Run long-poll in thread (it blocks on threading.Condition.wait)
    result = await asyncio.to_thread(
        search_engine.search_with_long_poll,
        query,
        last_version,
        limit,
        timeout,
    )

    return web.json_response(result)


async def handle_random_word(request):
    """GET /api/random-word — Get a random indexed word."""
    search_engine = request.app["search"]
    word = await asyncio.to_thread(search_engine.get_random_word)

    if word:
        return web.json_response({"word": word})
    return web.json_response({"word": None, "message": "No indexed words yet"})


async def handle_quiz_search(request):
    """
    GET /search?query=<word>&sortBy=relevance
    Quiz specific search route matching exact scoring formula:
    score = (frequency * 10) + 1000 - (depth * 5)
    Always queries SQLite database directly for best speed.
    """
    query = request.query.get("query", "").strip().lower()
    sort_by = request.query.get("sortBy", "")

    if not query:
        return web.json_response({"error": "query parameter is required"}, status=400)

    db = request.app["db"]
    read_conn = db.get_read_connection()

    try:
        sql = """
        SELECT
            it.token as word,
            it.url,
            it.origin_url as origin,
            it.depth,
            CAST(ROUND(it.tf * p.word_count) AS INTEGER) as frequency
        FROM index_tokens it
        JOIN pages p ON it.url = p.url
        WHERE it.token = ?
        """
        rows = read_conn.execute(sql, (query,)).fetchall()

        results = []
        for row in rows:
            freq = row["frequency"]
            depth = row["depth"]
            score = (freq * 10) + 1000 - (depth * 5)

            results.append({
                "word": row["word"],
                "url": row["url"],
                "origin": row["origin"],
                "depth": depth,
                "frequency": freq,
                "relevance_score": score
            })
    finally:
        read_conn.close()

    if sort_by == "relevance":
        results = sorted(results, key=lambda x: x["relevance_score"], reverse=True)

    return web.json_response({
        "query": query,
        "total": len(results),
        "results": results
    })
