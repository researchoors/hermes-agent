"""
Feed article store for HermesNative news feed.

Storage: ~/.hermes/digests/feed.json
Max articles: 1000 (oldest evicted on write)
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

FEED_DIR = Path(get_hermes_home()) / "digests"
FEED_FILE = FEED_DIR / "feed.json"
MAX_ARTICLES = 1000
# Sort floor for rows with an unparseable date — keeps them in the feed but
# never at the top.
_EPOCH_KEY = datetime.min.replace(tzinfo=timezone.utc).isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _read_feed() -> list[dict]:
    if not FEED_FILE.exists():
        return []
    try:
        with open(FEED_FILE, encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            return []
    except (json.JSONDecodeError, OSError):
        return []

def _write_feed(articles: list[dict]) -> None:
    FEED_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(FEED_DIR), suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(articles, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(FEED_FILE))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def _published_key(article: dict) -> str:
    """Sortable UTC key for an article's real publication date.

    published_ts arrives in mixed shapes ('...Z' from feeds/arXiv, '+00:00' or
    naive from datetime fallbacks), so we normalize through an aware datetime
    rather than sorting raw strings.

    Two failure modes, deliberately handled differently:
      * ABSENT date  -> fall back to ingest time. This is the documented
        fallback for sources that expose no date; treating it as "seen now" is
        the honest approximation.
      * MALFORMED date -> sink to epoch. A row we cannot parse must never be
        promoted to the top of the feed as if it were breaking news.
    """
    raw = (article.get("published_ts") or "").strip()
    if not raw:
        raw = (article.get("ts") or "").strip()
        if not raw:
            return _EPOCH_KEY
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return _EPOCH_KEY
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _by_published_desc(articles: list[dict]) -> list[dict]:
    """Newest-published first. Stable, so same-day items keep insertion order."""
    return sorted(articles, key=_published_key, reverse=True)


def _article_id(source: str, title: str, ts: str) -> str:
    import hashlib
    raw = f"{source}:{title}:{ts[:10]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def append_digest(source: str, articles: list[dict]) -> int:
    feed = _read_feed()
    now = _now_iso()
    existing_ids = {a["id"] for a in feed}
    new_articles = []
    for a in articles:
        # published_ts = when the SOURCE published it (real event date, may be
        # historical). ts = ingestion time (when it entered the feed) — kept for
        # stable sort/`since` semantics the client already relies on. Dedup keys
        # off published date so backdated re-runs don't create duplicates.
        published_ts = (a.get("published_ts") or "").strip() or now
        aid = _article_id(source, a.get("title", ""), published_ts)
        if aid in existing_ids:
            continue
        # content_type distinguishes papers from blog posts (client can badge/filter).
        content_type = (a.get("content_type") or "article").strip().lower()
        new_articles.append({
            "id": aid, "source": source,
            "title": a.get("title", ""), "url": a.get("url", ""),
            "summary": a.get("summary", "")[:500],
            "tags": a.get("tags", []), "image_url": a.get("image_url", ""),
            "ts": now,
            "published_ts": published_ts,
            # True when published_ts is a fetch-time guess (source exposed no
            # date), so the client can badge it rather than presenting an
            # inferred date as fact.
            "valid_time_approx": bool(a.get("valid_time_approx")),
            "content_type": content_type,
        })
    feed[:0] = new_articles
    # Order by real publication date BEFORE truncating, so eviction drops the
    # oldest-published article rather than the oldest-inserted one. Without this
    # a large backdated batch (arXiv/archive backfill) prepends historical rows
    # and evicts genuinely recent articles off the tail.
    feed = _by_published_desc(feed)[:MAX_ARTICLES]
    _write_feed(feed)
    return len(feed)


def get_feed(sources: Optional[list[str]] = None, since: Optional[str] = None,
             limit: int = 50, offset: int = 0) -> dict:
    feed = _read_feed()
    if sources:
        src_set = set(sources)
        feed = [a for a in feed if a["source"] in src_set]
    if since:
        # NOTE: `since` intentionally compares INGEST time, not published time.
        # A client polling "what's new since my last sync" must still receive
        # backdated articles (published months ago, ingested today); filtering
        # on published_ts would hide them below the watermark forever.
        feed = [a for a in feed if a["ts"] >= since]
    # Present newest-PUBLISHED first. The stored list is insertion-ordered, which
    # is not chronological once any backdated article is ingested, so ordering
    # must happen here — before offset/limit, or pagination slices a shuffled list.
    feed = _by_published_desc(feed)
    total = len(feed)
    limit = min(max(1, limit), 200)
    return {"articles": feed[offset:offset + limit], "total": total,
            "has_more": (offset + limit) < total}


def get_sources() -> dict:
    feed = _read_feed()
    counts: dict[str, int] = {}
    for a in feed:
        src = a["source"]
        counts[src] = counts.get(src, 0) + 1
    return {"sources": counts, "total": len(feed)}
