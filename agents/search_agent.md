# Search Agent

## Role

Search engineer responsible for the TF-IDF relevance ranking engine, index versioning, and the long-poll mechanism that delivers real-time result updates to clients while indexing is active.

## Responsibilities

- Implement `crawler/search.py`: `SearchEngine` class
- `search(query)`: tokenize query, retrieve matching postings, score via TF-IDF with title boost, return top-K `(url, origin_url, depth, score, title)` triples
- `wait_for_update(last_version, timeout)`: block on `threading.Condition` until `index_version` advances
- `search_with_long_poll()`: combined wait + search, primary entry point for the `/api/updates` endpoint
- Operate on a separate read-only SQLite connection to never block the crawler's write path
- AND semantics for multi-term queries (all tokens must be present)

## Prompt

> You are a search engineer. Implement `crawler/search.py`. Requirements: search(query) tokenizes query using the same tokenizer as the indexer, looks up index_tokens table for matching tokens (AND semantics — all tokens must be present), scores results using TF-IDF with 3x title boost, returns top-K results as list of {url, origin_url, depth, score, title}. Must run on a separate read-only SQLite connection (WAL mode guarantees no blocking). Long-poll: wait_for_update(last_version, timeout) blocks on threading.Condition until index_version advances. search_with_long_poll() combines both. No external search libraries.

## Key Outputs

- `SearchEngine.search()`: TF-IDF scoring in pure Python using `math.log`; IDF computed as `log(total_docs / docs_with_term)`, TF from `index_tokens.tf`, 3x multiplier if `in_title=1`
- AND semantics: finds intersection of document sets for all query tokens before scoring
- `wait_for_update()`: correctly uses `while` loop around `condition.wait()` to guard against spurious wakeups; checks deadline on each iteration
- `search_with_long_poll()`: returns `{updated: bool, index_version: int, results: [...]}` — the shape expected by the frontend
- `get_random_word()`: queries `index_tokens ORDER BY RANDOM() LIMIT 1` — powers the "Feeling Lucky" feature
- Separate `_read_conn` initialized via `db.get_read_connection()` in `init()` — never the write connection

## Decisions and Overrides

**Proposed:** Cache IDF scores in memory, refresh every N seconds, to avoid re-querying the database on every search.
**Decision:** Rejected. Fresh IDF query per search is fast enough at this scale (sub-10ms for up to ~50k documents with indexed lookups). A cache introduces staleness and invalidation complexity. The agent noted this would not scale past ~100k documents — accepted as out of scope.

**Proposed:** Use WebSockets instead of long-polling for real-time updates.
**Decision:** Rejected. Long-poll is specified in the PRD and simpler to implement correctly. WebSockets require a protocol upgrade path and persistent connection management that adds server complexity.

**Proposed:** Return snippet text (first 200 chars of body_text) in search results.
**Decision:** Accepted as an additive improvement — no downside, improves UI usability.

## Interfaces Consumed

- `Database.search_tokens(tokens, read_conn, limit) -> list[dict]`
- `Database.index_version` property
- `Database.index_condition` (`threading.Condition`)
- `tokenize()` from `crawler/parser.py`

## Interfaces Produced

- `SearchEngine.init()` — opens read connection
- `SearchEngine.search(query, limit=50) -> dict` — `{results, total, index_version, query}`
- `SearchEngine.wait_for_update(last_version, timeout) -> dict` — `{updated, index_version}`
- `SearchEngine.search_with_long_poll(query, last_version, limit, timeout) -> dict`
- `SearchEngine.get_random_word() -> str | None`
- `SearchEngine.close()` — closes read connection
