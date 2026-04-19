"""
CLI entry point for GoogleInOneDay crawler and search engine.

Subcommands:
    serve  — Start the web dashboard + API server
    crawl  — Run a headless crawl (no UI)
    search — CLI search query
    jobs   — List all crawl jobs
    export — Export indexed data to data/storage/p.data
"""

import argparse
import asyncio
import logging
import signal
import sys

from crawler.db import Database
from crawler.engine import CrawlEngine
from crawler.search import SearchEngine


def setup_logging(verbose: bool = False):
    """Configure structured logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress noisy aiohttp access logs
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="GoogleInOneDay",
        description="Concurrent web crawler and real-time search engine",
    )
    parser.add_argument(
        "--db", default="crawler.db", help="SQLite database path (default: crawler.db)"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # serve
    serve_parser = subparsers.add_parser("serve", help="Start the web dashboard + API server")
    serve_parser.add_argument(
        "--port", type=int, default=3600, help="Server port (default: 3600)"
    )
    serve_parser.add_argument(
        "--host", default="0.0.0.0", help="Server host (default: 0.0.0.0)"
    )

    # crawl
    crawl_parser = subparsers.add_parser("crawl", help="Run a headless crawl")
    crawl_parser.add_argument("url", help="Origin URL to crawl")
    crawl_parser.add_argument(
        "--depth", "-k", type=int, default=2, help="Max crawl depth (default: 2)"
    )
    crawl_parser.add_argument(
        "--rate", type=float, default=10.0, help="Max requests/second (default: 10)"
    )
    crawl_parser.add_argument(
        "--concurrent", type=int, default=20, help="Max concurrent requests (default: 20)"
    )

    # search
    search_parser = subparsers.add_parser("search", help="Search indexed pages")
    search_parser.add_argument("query", help="Search query string")
    search_parser.add_argument(
        "--limit", "-k", type=int, default=20, help="Max results (default: 20)"
    )

    # jobs
    subparsers.add_parser("jobs", help="List all crawl jobs")

    # export
    subparsers.add_parser("export", help="Export index data to data/storage/p.data")

    return parser


async def cmd_serve(args):
    """Start the web dashboard + API server."""
    from aiohttp import web
    from server.app import create_app

    db = Database(args.db)
    db.init()

    engine = CrawlEngine(db)
    search_engine = SearchEngine(db)
    search_engine.init()

    app = create_app(db, engine, search_engine)

    # Graceful shutdown handler
    loop = asyncio.get_event_loop()
    shutdown_triggered = False

    def handle_signal():
        nonlocal shutdown_triggered
        if not shutdown_triggered:
            shutdown_triggered = True
            logging.info("Shutdown signal received, stopping...")
            asyncio.create_task(cleanup(engine, search_engine, db))

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()

    print(f"\n  🕷️  GoogleInOneDay Crawler")
    print(f"  ────────────────────────")
    print(f"  Dashboard: http://localhost:{args.port}")
    print(f"  API:       http://localhost:{args.port}/api/")
    print(f"  Database:  {args.db}")
    print(f"\n  Press Ctrl+C to stop\n")

    # Keep server running
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await cleanup(engine, search_engine, db)
        await runner.cleanup()


async def cleanup(engine, search_engine, db):
    """Clean shutdown of all components."""
    try:
        await engine.shutdown()
    except Exception as e:
        logging.error("Engine shutdown error: %s", e)
    search_engine.close()
    db.close()


async def cmd_crawl(args):
    """Run a headless crawl."""
    db = Database(args.db)
    db.init()
    engine = CrawlEngine(db)

    print(f"Starting crawl: {args.url} (depth={args.depth}, rate={args.rate})")

    job_id = await engine.start_crawl(
        origin=args.url,
        depth=args.depth,
        max_rate=args.rate,
        max_concurrent=args.concurrent,
    )

    print(f"Job started: {job_id}")

    # Wait for completion
    try:
        while True:
            await asyncio.sleep(2)
            stats = engine.get_stats(job_id)
            if stats:
                status = stats.get("status", "unknown")
                pages = stats.get("pages_crawled", 0)
                queued = stats.get("urls_queued", 0)
                errors = stats.get("errors", 0)
                elapsed = stats.get("elapsed_seconds", 0)
                rate = stats.get("pages_per_second", 0)

                print(
                    f"  [{status}] Pages: {pages} | Queue: {queued} | "
                    f"Errors: {errors} | Rate: {rate:.1f} p/s | Elapsed: {elapsed:.0f}s",
                    end="\r",
                )

                if status in ("completed", "failed", "paused"):
                    print()
                    print(f"\nCrawl {status}. {pages} pages indexed.")
                    break
    except KeyboardInterrupt:
        print("\n\nInterrupted — shutting down gracefully...")
        await engine.shutdown()
        print("State saved. Resume later via the dashboard.")

    db.close()


def cmd_search(args):
    """Run a CLI search."""
    db = Database(args.db)
    db.init()
    search_engine = SearchEngine(db)
    search_engine.init()

    result = search_engine.search(args.query, limit=args.limit)

    print(f'\nResults for "{args.query}" ({result["total"]} hits, index v{result["index_version"]}):')
    print("─" * 70)

    for i, r in enumerate(result["results"], 1):
        print(f"{i:3}. {r.get('title', '(no title)')}")
        print(f"     URL:    {r['url']}")
        print(f"     Origin: {r['origin_url']} | Depth: {r['depth']} | Score: {r['score']:.4f}")
        print()

    if not result["results"]:
        print("  No results found.\n")

    search_engine.close()
    db.close()


def cmd_jobs(args):
    """List all crawl jobs."""
    db = Database(args.db)
    db.init()

    jobs = db.get_all_jobs()

    if not jobs:
        print("No crawl jobs found.")
        db.close()
        return

    print(f"\n{'JOB ID':<20} {'STATUS':<12} {'ORIGIN':<40} {'PAGES':<8} {'ERRORS':<8}")
    print("─" * 90)

    for job in jobs:
        origin = job["origin_url"]
        if len(origin) > 38:
            origin = origin[:35] + "..."
        print(
            f"{job['job_id']:<20} {job['status']:<12} {origin:<40} "
            f"{job['pages_crawled']:<8} {job['errors']:<8}"
        )

    print()
    db.close()


def cmd_export(args):
    """Export indexed data to data/storage/p.data."""
    from export_data import export_data
    export_data(args.db)


def main():
    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    setup_logging(args.verbose)

    if args.command == "serve":
        asyncio.run(cmd_serve(args))
    elif args.command == "crawl":
        asyncio.run(cmd_crawl(args))
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "jobs":
        cmd_jobs(args)
    elif args.command == "export":
        cmd_export(args)


if __name__ == "__main__":
    main()
