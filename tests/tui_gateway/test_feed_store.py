"""Regression tests for the news-feed producer path.

Guards against the failure diagnosed in the HermesNative feed: the reader RPCs
(``feed.get`` / ``feed.sources``) shipped without any code calling
``append_digest``, so the feed was permanently empty. These tests assert the
producer -> reader round-trip end to end, including the ``feed_publish`` tool
and dedup, so a future squash/rebase can't silently drop the writer again.
"""

import json

import pytest

from tui_gateway import digest_store


@pytest.fixture
def feed_file(tmp_path, monkeypatch):
    """Point the digest store at an isolated feed.json for each test.

    FEED_DIR / FEED_FILE are computed at import time, so patch the module
    globals directly rather than relying on HERMES_HOME import ordering.
    """
    d = tmp_path / "digests"
    monkeypatch.setattr(digest_store, "FEED_DIR", d)
    monkeypatch.setattr(digest_store, "FEED_FILE", d / "feed.json")
    return d / "feed.json"


class TestProducerReaderRoundTrip:
    def test_append_then_get_feed(self, feed_file):
        # The bug: readers existed but nothing populated the store.
        assert digest_store.get_feed()["articles"] == []

        digest_store.append_digest(
            "ai-digest",
            [
                {"title": "Model X released", "url": "https://ex/1", "summary": "s1"},
                {"title": "Chip Y benchmarks", "url": "https://ex/2", "summary": "s2"},
            ],
        )

        feed = digest_store.get_feed()
        assert feed["total"] == 2
        titles = {a["title"] for a in feed["articles"]}
        assert titles == {"Model X released", "Chip Y benchmarks"}
        # Producer-supplied fields survive the round-trip.
        first = feed["articles"][0]
        assert first["source"] == "ai-digest"
        assert first["url"] in {"https://ex/1", "https://ex/2"}
        assert "id" in first and "ts" in first

    def test_append_populates_sources(self, feed_file):
        digest_store.append_digest("ai-digest", [{"title": "a"}])
        digest_store.append_digest("markets", [{"title": "b"}, {"title": "c"}])

        sources = digest_store.get_sources()
        assert sources["total"] == 3
        assert sources["sources"] == {"ai-digest": 1, "markets": 2}

    def test_dedup_same_source(self, feed_file):
        digest_store.append_digest("ai-digest", [{"title": "dupe", "url": "u"}])
        digest_store.append_digest("ai-digest", [{"title": "dupe", "url": "u"}])
        # Re-publishing the same item must not create a second entry.
        assert digest_store.get_feed()["total"] == 1

    def test_source_filter_and_pagination(self, feed_file):
        digest_store.append_digest("ai-digest", [{"title": f"a{i}"} for i in range(3)])
        digest_store.append_digest("markets", [{"title": "m0"}])

        only_ai = digest_store.get_feed(sources=["ai-digest"])
        assert only_ai["total"] == 3
        assert all(a["source"] == "ai-digest" for a in only_ai["articles"])

        page = digest_store.get_feed(limit=2, offset=0)
        assert len(page["articles"]) == 2
        assert page["has_more"] is True


class TestFeedPublishTool:
    def test_tool_publishes_and_reports(self, feed_file):
        from tools.feed_tool import feed_publish

        out = json.loads(
            feed_publish("ai-digest", [{"title": "t1", "url": "u1"}])
        )
        assert out["source"] == "ai-digest"
        assert out["published"] == 1
        assert out["total"] == 1
        assert digest_store.get_feed()["total"] == 1

    def test_tool_rejects_bad_input(self, feed_file):
        from tools.feed_tool import feed_publish

        assert "error" in json.loads(feed_publish("", [{"title": "x"}]))
        assert "error" in json.loads(feed_publish("src", "not-a-list"))
        # An articles list with no usable objects is an error, not a silent no-op.
        assert "error" in json.loads(feed_publish("src", []))

    def test_tool_coerces_json_string_articles(self, feed_file):
        from tools.feed_tool import feed_publish

        # Some models emit each article as a JSON string rather than an object.
        out = json.loads(
            feed_publish("ai-digest", ['{"title": "coerced", "url": "u"}'])
        )
        assert out["published"] == 1
        assert digest_store.get_feed()["articles"][0]["title"] == "coerced"


class TestFeedToolRegistered:
    def test_feed_publish_is_registered(self):
        from tools.registry import registry

        # discover_builtin_tools imports every tools/*.py at startup; ensure the
        # module self-registered under the expected name + toolset.
        from tools import feed_tool  # noqa: F401  (import side effect: register)

        tool = registry._tools.get("feed_publish")
        assert tool is not None, "feed_publish tool must be registered"
        assert tool.toolset == "feed"


class TestPublishedDateOrdering:
    """The feed must be ordered by real publication date, not insertion order.

    Bug: ``feed.json`` is stored insertion-ordered and ``append_digest``
    prepends, so as soon as any backdated article is ingested (arXiv/archive
    backfill) the stored order stops being chronological. ``get_feed`` returned
    that raw slice, so the client showed a June article above an August one.
    """

    def test_backdated_article_does_not_jump_the_feed(self, feed_file):
        digest_store.append_digest(
            "news", [{"title": "recent", "published_ts": "2026-08-18T00:00:00Z"}]
        )
        # A historical batch arrives AFTER the recent item (the backfill case).
        digest_store.append_digest(
            "news",
            [
                {"title": "ancient", "published_ts": "2025-01-05T00:00:00Z"},
                {"title": "middle", "published_ts": "2026-04-01T00:00:00Z"},
            ],
        )
        titles = [a["title"] for a in digest_store.get_feed()["articles"]]
        assert titles == ["recent", "middle", "ancient"]

    def test_order_is_chronological_regardless_of_insertion(self, feed_file):
        # Insert deliberately shuffled so a passing result can't be insertion luck.
        digest_store.append_digest(
            "news",
            [
                {"title": "c", "published_ts": "2026-03-01T00:00:00Z"},
                {"title": "a", "published_ts": "2026-07-01T00:00:00Z"},
                {"title": "b", "published_ts": "2026-05-01T00:00:00Z"},
            ],
        )
        keys = [
            digest_store._published_key(a)
            for a in digest_store.get_feed()["articles"]
        ]
        assert keys == sorted(keys, reverse=True)

    def test_mixed_timestamp_formats_sort_correctly(self, feed_file):
        # Producers emit '...Z' (feeds/arXiv), '+00:00' (isoformat), and naive
        # stamps. Sorting raw strings across these shapes is not safe.
        digest_store.append_digest(
            "news",
            [
                {"title": "zulu", "published_ts": "2026-08-19T04:00:00Z"},
                {"title": "offset", "published_ts": "2026-08-19T05:00:00+00:00"},
                {"title": "naive", "published_ts": "2026-08-19T06:00:00"},
            ],
        )
        titles = [a["title"] for a in digest_store.get_feed()["articles"]]
        assert titles == ["naive", "offset", "zulu"]

    def test_pagination_slices_the_ordered_list(self, feed_file):
        # Ordering must happen before offset/limit, else pages overlap or drop
        # rows. Assert the invariant: page1 + page2 == the full ordered list.
        digest_store.append_digest(
            "news",
            [
                {"title": f"a{i}", "published_ts": f"2026-0{i + 1}-01T00:00:00Z"}
                for i in range(6)
            ],
        )
        full = [a["id"] for a in digest_store.get_feed(limit=6)["articles"]]
        page1 = [a["id"] for a in digest_store.get_feed(limit=3, offset=0)["articles"]]
        page2 = [a["id"] for a in digest_store.get_feed(limit=3, offset=3)["articles"]]
        assert page1 + page2 == full
        assert len(set(page1) & set(page2)) == 0

    def test_eviction_drops_oldest_published_not_oldest_inserted(
        self, feed_file, monkeypatch
    ):
        monkeypatch.setattr(digest_store, "MAX_ARTICLES", 3)
        digest_store.append_digest(
            "news", [{"title": "fresh", "published_ts": "2026-08-18T00:00:00Z"}]
        )
        # A flood of ancient articles must not evict genuinely recent news.
        digest_store.append_digest(
            "news",
            [
                {"title": f"old{i}", "published_ts": f"2020-01-0{i + 1}T00:00:00Z"}
                for i in range(5)
            ],
        )
        titles = [a["title"] for a in digest_store.get_feed()["articles"]]
        assert "fresh" in titles
        assert len(titles) == 3


class TestPublishedDateSemantics:
    def test_absent_date_falls_back_to_ingest_time(self, feed_file):
        digest_store.append_digest("news", [{"title": "no-date"}])
        article = digest_store.get_feed()["articles"][0]
        # Undated items are stamped at ingest so they stay sortable.
        assert article["published_ts"] == article["ts"]

    def test_malformed_date_sinks_instead_of_topping_the_feed(self, feed_file):
        digest_store.append_digest(
            "news",
            [
                {"title": "good", "published_ts": "2026-01-01T00:00:00Z"},
                {"title": "garbage", "published_ts": "not-a-date"},
            ],
        )
        titles = [a["title"] for a in digest_store.get_feed()["articles"]]
        # An unparseable date must never be promoted as if it were breaking news.
        assert titles[-1] == "garbage"

    def test_since_uses_ingest_time_so_backdated_items_are_delivered(self, feed_file):
        # A client polling "what's new since my last sync" must still receive an
        # article published long ago but ingested now; filtering `since` on
        # published_ts would hide it below the watermark permanently.
        digest_store.append_digest(
            "news", [{"title": "old-news", "published_ts": "2025-02-01T00:00:00Z"}]
        )
        ingest_ts = digest_store.get_feed()["articles"][0]["ts"]
        got = digest_store.get_feed(since=ingest_ts)
        assert [a["title"] for a in got["articles"]] == ["old-news"]

    def test_approximate_flag_round_trips(self, feed_file):
        # Sources with no date on the index page get fetch-time stamped; the
        # client needs the flag to mark the date rather than assert it.
        digest_store.append_digest(
            "news",
            [
                {"title": "guessed", "valid_time_approx": True},
                {"title": "known", "published_ts": "2026-01-01T00:00:00Z"},
            ],
        )
        flags = {
            a["title"]: a["valid_time_approx"]
            for a in digest_store.get_feed()["articles"]
        }
        assert flags == {"guessed": True, "known": False}

    def test_dedup_is_stable_across_days_for_the_same_article(self, feed_file):
        # Dedup keys off the publication date, so re-ingesting the same article
        # on a later day must not create a duplicate.
        art = {"title": "stable", "published_ts": "2026-06-11T00:00:00Z"}
        digest_store.append_digest("news", [art])
        digest_store.append_digest("news", [art])
        assert digest_store.get_feed()["total"] == 1
