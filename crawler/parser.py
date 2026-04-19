"""
HTML parser and text tokenizer using stdlib html.parser (zero dependencies).

Extracts from HTML pages:
- <a href> links (normalized via urllib.parse)
- <title> text
- Visible body text (excluding <script>, <style>, <noscript> content)

Tokenizes text into normalized words with frequency counts for TF scoring.
"""

import re
import hashlib
import logging
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse
from collections import Counter

logger = logging.getLogger(__name__)

# Tags whose text content should be ignored (not visible to users)
IGNORED_TAGS = {"script", "style", "noscript", "meta", "link", "head"}

# Common English stop words — removed from token index
STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "shall", "can", "need",
    "must", "it", "its", "this", "that", "these", "those", "i", "me",
    "my", "we", "us", "our", "you", "your", "he", "him", "his", "she",
    "her", "they", "them", "their", "what", "which", "who", "whom",
    "when", "where", "why", "how", "all", "each", "every", "both",
    "few", "more", "most", "other", "some", "such", "no", "not", "only",
    "own", "same", "so", "than", "too", "very", "just", "because",
    "about", "up", "out", "if", "then", "also", "into", "over", "after",
    "before", "between", "under", "again", "further", "once", "here",
    "there", "any", "am", "nor", "don", "t", "s", "d", "ll", "ve", "re",
    "didn", "doesn", "hadn", "hasn", "haven", "isn", "wasn", "weren",
    "won", "wouldn", "couldn", "shouldn", "ain", "aren", "mustn",
})

# Regex for tokenization: split on non-alphanumeric characters
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


class LinkTextExtractor(HTMLParser):
    """
    HTMLParser subclass that extracts links, title, and visible body text.

    Usage:
        extractor = LinkTextExtractor(base_url="https://example.com/page")
        extractor.feed(html_string)
        links = extractor.links          # list of normalized absolute URLs
        title = extractor.title          # page title string
        body_text = extractor.body_text  # visible text content
    """

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url

        # Results
        self.links: list[str] = []
        self.title: str = ""
        self.body_text: str = ""

        # Internal state
        self._in_title = False
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []
        self._ignore_depth = 0  # depth inside ignored tags

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        tag_lower = tag.lower()

        # Track ignored tags
        if tag_lower in IGNORED_TAGS:
            self._ignore_depth += 1
            return

        # Title tag
        if tag_lower == "title":
            self._in_title = True
            return

        # Extract links from <a href="...">
        if tag_lower == "a":
            for attr_name, attr_value in attrs:
                if attr_name.lower() == "href" and attr_value:
                    normalized = normalize_url(attr_value, self.base_url)
                    if normalized:
                        self.links.append(normalized)

    def handle_endtag(self, tag: str):
        tag_lower = tag.lower()

        if tag_lower in IGNORED_TAGS:
            self._ignore_depth = max(0, self._ignore_depth - 1)
            return

        if tag_lower == "title":
            self._in_title = False

    def handle_data(self, data: str):
        if self._in_title:
            self._title_parts.append(data)

        if self._ignore_depth == 0:
            self._text_parts.append(data)

    def close(self):
        """Finalize parsing — assemble title and body text."""
        super().close()
        self.title = " ".join(self._title_parts).strip()
        self.body_text = " ".join(self._text_parts).strip()


def normalize_url(url: str, base_url: str) -> str | None:
    """
    Normalize a URL for consistent deduplication.

    Rules:
    1. Resolve relative paths via urljoin.
    2. Remove fragment identifiers (#section).
    3. Strip trailing slashes.
    4. Canonicalize scheme and host to lowercase.
    5. Filter to http:// and https:// only.

    Returns None if the URL should be filtered out.
    """
    try:
        # Resolve relative URL
        absolute = urljoin(base_url, url.strip())

        # Parse
        parsed = urlparse(absolute)

        # Only http and https
        if parsed.scheme.lower() not in ("http", "https"):
            return None

        # Remove fragment
        cleaned = parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            fragment="",
        )

        # Reconstruct
        result = urlunparse(cleaned)

        # Strip trailing slash (but keep root "/" paths)
        if result.endswith("/") and len(urlparse(result).path) > 1:
            result = result.rstrip("/")

        return result

    except Exception:
        return None


def parse_html(html: str, base_url: str) -> dict:
    """
    Parse an HTML page and extract links, title, body text.

    Returns dict with keys:
        title: str
        body_text: str
        links: list[str] (normalized absolute URLs)
        content_hash: str (SHA-256 of body text)
    """
    try:
        extractor = LinkTextExtractor(base_url)
        extractor.feed(html)
        extractor.close()

        content_hash = hashlib.sha256(extractor.body_text.encode("utf-8")).hexdigest()

        return {
            "title": extractor.title,
            "body_text": extractor.body_text,
            "links": list(set(extractor.links)),  # deduplicate
            "content_hash": content_hash,
        }
    except Exception as e:
        logger.warning("Failed to parse HTML from %s: %s", base_url, e)
        return {
            "title": "",
            "body_text": "",
            "links": [],
            "content_hash": "",
        }


def tokenize(text: str) -> Counter:
    """
    Tokenize text into normalized word frequencies.

    Rules:
    1. Lowercase all text.
    2. Split on non-alphanumeric characters.
    3. Filter tokens shorter than 2 characters.
    4. Remove stop words.

    Returns Counter mapping token -> count.
    """
    words = _TOKEN_SPLIT_RE.split(text.lower())
    filtered = [w for w in words if len(w) >= 2 and w not in STOP_WORDS]
    return Counter(filtered)


def compute_tokens(
    body_text: str, title: str
) -> tuple[dict[str, tuple[float, bool]], int]:
    """
    Compute token data for indexing.

    Returns:
        tokens: dict mapping token -> (tf_score, in_title)
        word_count: total number of tokens in the document
    """
    body_counts = tokenize(body_text)
    title_counts = tokenize(title)

    # Merge: body tokens with title flag
    all_tokens = set(body_counts.keys()) | set(title_counts.keys())
    total_words = sum(body_counts.values()) + sum(title_counts.values())

    if total_words == 0:
        return {}, 0

    tokens = {}
    for token in all_tokens:
        count = body_counts.get(token, 0) + title_counts.get(token, 0)
        tf = count / total_words
        in_title = token in title_counts
        tokens[token] = (tf, in_title)

    return tokens, total_words
