"""
TF-IDF search engine implementing search(query).

Returns (url, origin_url, depth) triples ranked by relevance.
Operates on a separate read-only SQLite connection (WAL allows concurrent reads).
Manages index versioning and the long-poll threading.Condition.
"""

import time
import threading
import logging

from crawler.db import Database
from crawler.parser import tokenize

logger = logging.getLogger(__name__)


class SearchEngine:
    """
    Search engine for indexed web pages.

    Uses TF-IDF scoring with title boost (3x) and AND semantics
    for multi-term queries. Runs on a separate read-only SQLite
    connection so search never blocks the crawler.

    Long-poll support:
        Clients call wait_for_update(last_version, timeout) which blocks
        until index_version advances or timeout is reached.
    """

    def __init__(self, db: Database):
        self.db = db
        self._read_conn = None

    def init(self):
        """Initialize the read-only connection for search queries."""
        self._read_conn = self.db.get_read_connection()
        logger.info("Search engine initialized with read-only connection")

    def close(self):
        """Close the read connection."""
        if self._read_conn:
            self._read_conn.close()
            self._read_conn = None

    def search(self, query: str, limit: int = 50) -> dict:
        """
        Search indexed pages for the given query.

        Args:
            query: search string (multi-word queries use AND semantics)
            limit: max results to return

        Returns:
            dict with keys:
                results: list of {url, origin_url, depth, score, title}
                total: number of matching documents
                index_version: current index version
                query: the original query string
        """
        # Tokenize query using same tokenizer as indexer
        tokens = list(tokenize(query).keys())

        if not tokens:
            return {
                "results": [],
                "total": 0,
                "index_version": self.db.index_version,
                "query": query,
            }

        # Run search on read connection
        results = self.db.search_tokens(tokens, self._read_conn, limit=limit)

        return {
            "results": results,
            "total": len(results),
            "index_version": self.db.index_version,
            "query": query,
        }

    def wait_for_update(self, last_version: int, timeout: float = 30.0) -> dict:
        """
        Long-poll: block until index_version advances past last_version or timeout.

        Uses threading.Condition to efficiently wait without busy-polling.
        Always re-checks version in a while loop to guard against spurious wakeups.

        Args:
            last_version: the client's last seen index version
            timeout: max seconds to wait

        Returns:
            dict with keys: updated (bool), index_version (int)
        """
        deadline = time.time() + timeout

        with self.db.index_condition:
            while self.db.index_version <= last_version:
                remaining = deadline - time.time()
                if remaining <= 0:
                    # Timeout reached — return without update
                    return {
                        "updated": False,
                        "index_version": self.db.index_version,
                    }
                # Wait for notification or timeout
                self.db.index_condition.wait(timeout=remaining)

        # Version has advanced — return success
        return {
            "updated": True,
            "index_version": self.db.index_version,
        }

    def search_with_long_poll(
        self,
        query: str,
        last_version: int,
        limit: int = 50,
        timeout: float = 30.0,
    ) -> dict:
        """
        Combined long-poll + search: wait for update, then return fresh results.

        This is the main entry point for the /api/updates endpoint.
        """
        # Wait for index to advance
        update_result = self.wait_for_update(last_version, timeout)

        if not update_result["updated"]:
            return {
                "updated": False,
                "index_version": update_result["index_version"],
                "results": [],
                "total": 0,
                "query": query,
            }

        # Index has updated — re-run search with fresh data
        search_result = self.search(query, limit)
        search_result["updated"] = True
        return search_result

    def get_random_word(self) -> str | None:
        """Get a random indexed token for 'Feeling Lucky' feature."""
        if not self._read_conn:
            return None

        row = self._read_conn.execute(
            "SELECT token FROM index_tokens ORDER BY RANDOM() LIMIT 1"
        ).fetchone()
        return row["token"] if row else None
