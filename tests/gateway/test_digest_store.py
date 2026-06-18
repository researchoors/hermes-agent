"""Regression tests for tui_gateway.digest_store article ID handling."""
import importlib

import pytest


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Load digest_store with its feed file redirected into a temp dir."""
    import tui_gateway.digest_store as ds
    importlib.reload(ds)
    feed_dir = tmp_path / "digests"
    monkeypatch.setattr(ds, "FEED_DIR", feed_dir)
    monkeypatch.setattr(ds, "FEED_FILE", feed_dir / "feed.json")
    return ds


def test_titleless_batch_keeps_every_item(store):
    """A batch of tweets (no title) must not collapse to one article.

    Regression: ids were hashed from source:title:capture_date, so a whole
    run of title-less items shared one id and all but one were dropped.
    """
    tweets = [
        {"title": "", "summary": f"tweet number {i}", "url": f"https://x.com/u/status/{i}"}
        for i in range(27)
    ]
    count = store.append_digest("twitter", tweets)
    assert count == 27

    feed = store.get_feed(sources=["twitter"])
    assert feed["total"] == 27
    ids = [a["id"] for a in feed["articles"]]
    assert len(set(ids)) == 27, "every tweet must get a distinct id"


def test_genuine_duplicates_are_deduped(store):
    """Re-appending the same items (same url) should not create new rows."""
    items = [{"title": "", "summary": "same", "url": "https://x.com/u/status/1"}]
    store.append_digest("twitter", items)
    store.append_digest("twitter", items)
    assert store.get_feed(sources=["twitter"])["total"] == 1


def test_titleless_no_url_dedupes_on_content(store):
    """With no url, identical content dedupes but distinct content doesn't."""
    store.append_digest("twitter", [{"title": "", "summary": "a"}])
    store.append_digest("twitter", [{"title": "", "summary": "a"}])  # dup
    store.append_digest("twitter", [{"title": "", "summary": "b"}])  # new
    assert store.get_feed(sources=["twitter"])["total"] == 2
