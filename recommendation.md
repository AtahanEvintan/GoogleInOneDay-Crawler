# Production Deployment Recommendations

## Current Architecture

The system runs as a single Python process on localhost, using SQLite for storage, asyncio for concurrency, and a single aiohttp web server for both API and static file serving. This architecture is adequate for tens of thousands of pages on a single machine.

## Recommended Next Steps for Production

**Storage tier migration.** Replace SQLite with PostgreSQL (or CockroachDB for multi-region). The existing SQL schema maps directly — change the connection string and swap `sqlite3` calls for `asyncpg`. This is a migration, not a rewrite. For the search index specifically, introduce Elasticsearch or OpenSearch: our TF-IDF scoring logic maps cleanly to Elasticsearch's BM25, yielding sub-10ms query latency at millions of documents with built-in faceting, fuzzy matching, and relevance tuning that SQLite cannot provide.

**Crawl coordination and horizontal scaling.** Replace the in-process `asyncio.Queue` frontier with a distributed message broker — Redis Streams or Apache Kafka. The current producer-consumer pipeline (frontier → fetch → parse → index) maps directly to topic-based message routing. Crawl workers become stateless consumers that can scale horizontally across machines. Add a centralized frontier coordinator for global URL deduplication (Redis SET or Bloom filter), per-domain politeness enforcement, and priority-based URL scheduling. Each worker region maintains its own rate limiters; the coordinator manages the global crawl budget.

**Rate limiting and backpressure at scale.** Move the token-bucket rate limiter from in-process to a Redis-backed distributed rate limiter using sliding window counters (`INCR`/`EXPIRE`). This allows multiple crawler instances to share a single rate budget per target domain. Backpressure signals should flow from the index writer back through the message broker's consumer group lag metrics (Kafka consumer lag or Redis Stream pending entries), triggering automatic worker scaling or throttling.

**Observability.** Replace the custom `/api/status` endpoint with Prometheus metric exposition (`prometheus_client` library) and Grafana dashboards. Key metrics: pages/sec, queue depth per stage, p50/p95/p99 fetch latency, error rate by domain, index commit latency, and search query latency. Route structured logs (JSON lines) to an ELK stack (Elasticsearch, Logstash, Kibana) or CloudWatch for centralized log search and alerting. Add distributed tracing (OpenTelemetry) to track a URL's journey from frontier to indexed page.

**Real-time updates at scale.** Replace long-polling with Server-Sent Events (SSE) or WebSockets for lower latency and reduced connection overhead. Back the notification layer with Redis Pub/Sub so multiple web server instances can fan out index update events to connected clients. For very large deployments, consider a dedicated event streaming layer (Kafka → WebSocket gateway).

**Resilience and operational safety.** Add circuit breakers around external HTTP fetching (per-domain) to prevent cascading failures from unresponsive hosts. Implement dead-letter queues for URLs that fail repeatedly. Add health checks and readiness probes for Kubernetes deployment. Use blue-green or canary deployments for crawler code updates to avoid disrupting active crawls. Persist crawler checkpoints to object storage (S3/GCS) for disaster recovery beyond the database.
