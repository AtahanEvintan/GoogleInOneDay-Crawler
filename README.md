# GoogleInOneDay — Web Crawler & Search Engine (Project 2)

A concurrent web crawler and real-time search engine built in Python using a multi-agent AI development workflow. Five specialized agents — Architect, Crawler, Indexer, Search, and UI — each owned a distinct domain of the system. This document describes the final system; see [multi_agent_workflow.md](multi_agent_workflow.md) for the full agent collaboration process and [agents/](agents/) for per-agent descriptions.

## Features

- **Concurrent BFS Crawler** — Asyncio-powered multi-stage pipeline (Frontier → Fetch → Parse → Index) with configurable depth, rate limiting, and concurrency control
- **Relevance Search** — Relevance keyword search with title boost, AND semantics for multi-term queries, and live result updates via long-polling
- **Backpressure Controls** — Token-bucket rate limiter, asyncio.Semaphore concurrency cap, and bounded queue depth prevent system overload
- **Crash-Safe Persistence** — SQLite WAL mode with transactions ensures no data loss on interruption; crawls resume automatically
- **Premium Web Dashboard** — Dark-theme glassmorphism UI with live metrics, job management, and real-time search
- **Live Log Stream** — Auto-scrolling terminal UI streams crawler activity, discoveries, and HTTP rate limits per job in real-time
- **Multiple Concurrent Jobs** — Run several crawls simultaneously with independent backpressure controls
- **Pause/Resume/Stop** — Full job lifecycle management from the UI or CLI

## Quick Start

### Prerequisites
- Python 3.11+
- pip

### Installation

```bash
git clone https://github.com/yourusername/GoogleInOneDay-Crawler2.git
cd GoogleInOneDay-Crawler2
pip install -r requirements.txt
```

### Start the Dashboard

```bash
python main.py serve
```

Open **http://localhost:3600** in your browser.

### CLI Usage

```bash
# Headless crawl
python main.py crawl https://quotes.toscrape.com --depth 2 --rate 5

# Search from CLI
python main.py search "life"

# List all jobs
python main.py jobs
```

## Architecture

```
GoogleInOneDay-Crawler2/
├── crawler/              # Core engine
│   ├── db.py             # SQLite WAL — schema, CRUD, index versioning
│   ├── fetcher.py        # Async HTTP client, rate limiter, semaphore
│   ├── parser.py         # html.parser — link/text extraction, tokenizer
│   ├── engine.py         # BFS pipeline — index(origin, k)
│   └── search.py         # TF-IDF search — search(query) + long-poll
├── server/
│   └── app.py            # aiohttp web server — API + static serving
├── static/               # Premium dark-theme dashboard
│   ├── index.html
│   ├── style.css
│   └── app.js
├── main.py               # CLI entry point
├── product_prd.md        # Product requirements document
├── recommendation.md     # Production deployment recommendations
├── multi_agent_workflow.md  # Multi-agent development process
├── agents/               # Per-agent description files
│   ├── architect_agent.md
│   ├── crawler_agent.md
│   ├── indexer_agent.md
│   ├── search_agent.md
│   └── ui_agent.md
├── requirements.txt      # aiohttp (single external dependency)
└── export_data.py        # Export indexed data to data/storage/p.data
```

### How It Works

1. **Crawler** receives a seed URL and depth `k`. It performs BFS, fetching pages concurrently via asyncio, parsing HTML with stdlib `html.parser`, and indexing tokens into SQLite.

2. **Backpressure** operates at three levels: token-bucket rate limiter (req/sec), asyncio.Semaphore (max concurrent HTTP requests), and bounded asyncio.Queue (max pending URLs).

3. **Search** tokenizes the query, looks up the inverted index (`index_tokens` table), computes relevance scores, and returns `(url, origin_url, depth)` triples ranked by relevance.

4. **Long-polling** uses `threading.Condition`: the indexer calls `notify_all()` after each batch commit, waking search clients who re-run their query against fresh data.

5. **Persistence** uses SQLite WAL mode: concurrent reads (search) never block writes (indexing). On interruption, pending URLs stay in the queue table for automatic resumption.

6. **Export** uses `export_data.py` to export indexed data to `data/storage/p.data` for offline analysis.

## API Endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/api/crawl` | Start a crawl `{"origin": "url", "depth": N}` |
| GET | `/api/jobs` | List all jobs |
| GET | `/api/jobs/{id}` | Job status |
| POST | `/api/jobs/{id}/pause` | Pause a job |
| POST | `/api/jobs/{id}/resume` | Resume a job |
| POST | `/api/jobs/{id}/stop` | Stop a job |
| GET | `/api/status` | Global system stats |
| GET | `/api/search?q=...` | Search indexed pages |
| GET | `/api/updates?q=...&last_version=V` | Long-poll for updates |
| GET | `/api/random-word` | Random indexed word |

## Configuration

| Parameter | Default | Description |
|---|---|---|
| `--port` | 3600 | Web server port |
| `--depth` | 2 | Max crawl depth |
| `--rate` | 10 | Requests per second |
| `--concurrent` | 20 | Max simultaneous HTTP requests |
| `max_queue` | 10000 | Max URLs in frontier queue |

## Dependencies

- **aiohttp** — async HTTP client + web server (single external dependency)
- Everything else uses Python standard library: `sqlite3`, `html.parser`, `urllib.parse`, `asyncio`, `threading`, `json`, `re`, `hashlib`, `logging`

## License

MIT
