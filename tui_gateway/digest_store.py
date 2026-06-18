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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _read_feed() -> list[dict]:
    if not FEED_FILE.exists():
        return []
    try:
        with open(FEED_FILE) as f:
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

def _article_id(source: str, article: dict) -> str:
    """Stable, collision-resistant ID for a feed article.

    Derived from the article's own identifying content — URL when present
    (the natural unique key), otherwise the full title+summary text. The
    previous scheme hashed ``source:title:capture_date``, which collapsed
    every title-less item from one run (e.g. tweets, which have no title)
    into a single ID; the feed JSON then held N rows sharing one ``id`` and
    the client's Identifiable ForEach rendered only one of them. Hashing
    real content gives each tweet a distinct ID while still deduping genuine
    repeats across runs.
    """
    import hashlib
    url = (article.get("url") or "").strip()
    if url:
        key = f"{source}:{url}"
    else:
        title = article.get("title", "")
        summary = article.get("summary", "")
        key = f"{source}:{title}:{summary}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def append_digest(source: str, articles: list[dict]) -> int:
    feed = _read_feed()
    now = _now_iso()
    existing_ids = {a["id"] for a in feed}
    new_articles = []
    for a in articles:
        aid = _article_id(source, a)
        # Skip items already in the stored feed AND duplicates within this
        # same batch (existing_ids is updated as we go), so a feed never holds
        # two rows with the same id — which the client would otherwise collapse.
        if aid in existing_ids:
            continue
        existing_ids.add(aid)
        new_articles.append({
            "id": aid, "source": source,
            "title": a.get("title", ""), "url": a.get("url", ""),
            "summary": a.get("summary", "")[:500],
            "tags": a.get("tags", []), "image_url": a.get("image_url", ""),
            "ts": a.get("ts") or now,
        })
    feed[:0] = new_articles
    feed = feed[:MAX_ARTICLES]
    _write_feed(feed)
    return len(feed)


def get_feed(sources: Optional[list[str]] = None, since: Optional[str] = None,
             limit: int = 50, offset: int = 0) -> dict:
    feed = _read_feed()
    if sources:
        src_set = set(sources)
        feed = [a for a in feed if a["source"] in src_set]
    if since:
        feed = [a for a in feed if a["ts"] >= since]
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
