"""The ETag store and the 304 signal."""

from collector_core.conditional import (
    ETAGS,
    ETagStore,
    UpstreamUnchanged,
    conditional_headers,
)


def test_a_stored_etag_becomes_an_if_none_match_header():
    store = ETagStore()
    store.set("http://x/doc.csv", 'W/"abc"')
    assert conditional_headers("http://x/doc.csv", store) == {
        "If-None-Match": 'W/"abc"'
    }


def test_an_unknown_key_sends_no_conditional_header():
    """A first-ever fetch must be an ordinary unconditional GET."""
    assert conditional_headers("http://x/never-seen.csv", ETagStore()) == {}


def test_setting_none_forgets_the_key_rather_than_storing_a_null():
    """An upstream that stops sending ETags must fall back to unconditional
    GETs, not send `If-None-Match: None` forever."""
    store = ETagStore()
    store.set("k", 'W/"abc"')
    store.set("k", None)
    assert store.get("k") is None
    assert conditional_headers("k", store) == {}


def test_clear_empties_the_store():
    store = ETagStore()
    store.set("k", 'W/"abc"')
    store.clear()
    assert store.get("k") is None


def test_the_module_singleton_is_an_etag_store():
    assert isinstance(ETAGS, ETagStore)


def test_upstream_unchanged_carries_the_url_and_the_source_ref():
    exc = UpstreamUnchanged("http://x/doc.csv", source_ref='W/"abc"')
    assert exc.url == "http://x/doc.csv"
    assert exc.source_ref == 'W/"abc"'
    assert "304" in str(exc)
