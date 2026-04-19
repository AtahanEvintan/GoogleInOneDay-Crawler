# Indexer Agent

## Role

Data engineer responsible for HTML parsing, text extraction, tokenization, and the SQLite database layer. Owns the inverted index schema, batch write logic, visited-set deduplication, and the index versioning mechanism used by the long-poll system.

## Responsibilities

- Implement `crawler/parser.py`: `HTMLParser` subclass for link and text extraction; tokenizer with stop word removal and TF computation
- Implement `crawler/db.py`: SQLite schema, WAL configuration, all CRUD operations, connection management, index versioning
- Ensure concurrent reads (search) never block writes (indexing) via WAL mode
- Batch inserts via `executemany` for throughput
- `threading.Condition` on the `Database` object for long-poll notification
- URL normalization: resolve relative paths, strip fragments, lowercase scheme/host, filter non-http/https

## Prompt

> You are a Python engineer specializing in text processing and databases. Implement `crawler/parser.py` and `crawler/db.py`. Parser requirements: use html.parser.HTMLParser subclass, extract visible body text (exclude script/style/noscript), extract page title, extract and normalize anchor hrefs (resolve relative paths, strip fragments, filter to http/https). Tokenizer: lowercase, split on non-alphanumeric, remove stop words, compute term frequencies. DB requirements: SQLite WAL mode, tables for pages, queue, index_tokens, crawl_jobs, system_meta. Batch insert for index_tokens. Monotonically incrementing index_version in system_meta. threading.Condition for long-poll notification on each batch commit. No ORM — raw sqlite3 only.

## Key Outputs

- `HTMLParser` subclass with tag-depth tracking to suppress text inside `<script>`, `<style>`, `<noscript>` tags
- Tokenizer: lowercase, `re.split(r'[^a-z0-9]+', text)`, filter tokens < 2 chars, hard-coded stop word set (~150 words), returns `{token: frequency}` dict
- `compute_tokens()`: merges title tokens (3x weight for TF) and body tokens into a single posting map
- `Database` class: WAL mode, `PRAGMA busy_timeout=5000`, single write connection (`_write_conn`), `get_read_connection()` for search
- `insert_pages_batch()`: single transaction wrapping both `pages` and `index_tokens` inserts via `executemany`
- `index_version`: stored in `system_meta` as integer string; `increment_index_version()` increments and calls `index_condition.notify_all()`
- `bulk_check_visited()`: single `SELECT url FROM pages WHERE url IN (...)` query for efficient deduplication

## Decisions and Overrides

**Proposed:** `INSERT OR IGNORE` for duplicate token entries.
**Decision:** Changed to `INSERT OR REPLACE` so re-crawled pages update their TF scores. Agent flagged write amplification; accepted since deduplication logic prevents most re-crawls.

**Proposed:** SQLite FTS5 virtual table for full-text search.
**Decision:** Declined. FTS5 availability varies across SQLite builds; manual inverted index keeps the project stdlib-compliant and the TF-IDF logic explicit and auditable.

**Proposed:** Separate `visited` table for deduplication to avoid scanning `pages`.
**Decision:** Declined. `pages.url` is a PRIMARY KEY with an implicit B-tree index — lookups are O(log n). A separate table adds write overhead with no lookup benefit.

## Interfaces Consumed

- Python stdlib: `html.parser`, `urllib.parse`, `sqlite3`, `threading`, `hashlib`, `re`, `math`, `collections`

## Interfaces Produced

- `parse_html(html: str, base_url: str) -> dict` — `{title, body_text, links, content_hash}`
- `compute_tokens(body_text: str, title: str) -> tuple[dict, int]` — `(token_freq_map, word_count)`
- `tokenize(text: str) -> dict` — `{token: frequency}`
- `Database` class with: `create_job()`, `get_job()`, `get_all_jobs()`, `update_job_stats()`, `set_job_status()`, `is_visited()`, `bulk_check_visited()`, `enqueue_urls()`, `dequeue_pending()`, `mark_queue_done()`, `insert_pages_batch()`, `increment_index_version()`, `search_tokens()`, `get_read_connection()`
- `Database.index_version` property (int)
- `Database.index_condition` (`threading.Condition`)
