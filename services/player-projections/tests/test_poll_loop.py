import asyncio

import pytest

from player_projections import main

URL_TEMPLATE = "https://example.test/{format}.json"


@pytest.fixture(autouse=True)
def reset_state():
    """_state is module-global; reset every format around every test."""
    for fmt in main.FORMATS:
        main._state[fmt] = main._empty_cache()
    yield
    for fmt in main.FORMATS:
        main._state[fmt] = main._empty_cache()


@pytest.fixture
def one_iteration(monkeypatch):
    """Make the infinite poll loop run exactly one pass, then stop."""

    async def stop_after_first(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", stop_after_first)


async def test_stub_mode_returns_immediately_without_polling(monkeypatch):
    """An empty PROJECTIONS_SNAPSHOT_URL exits before calling upstream at all."""
    monkeypatch.setenv("PROJECTIONS_SNAPSHOT_URL", "")
    called = []

    async def record(url, expect_format=None):
        called.append(url)
        return []

    monkeypatch.setattr(main, "fetch_projections", record)

    await main._poll_loop()

    assert called == []
    assert all(main._state[f]["upstream_healthy"] is False for f in main.FORMATS)


async def test_one_pass_fetches_every_format(monkeypatch, one_iteration):
    """A single iteration polls all three documents, not just one."""
    monkeypatch.setenv("PROJECTIONS_SNAPSHOT_URL", URL_TEMPLATE)
    requested = []

    async def fake_fetch(url, expect_format=None):
        requested.append((url, expect_format))
        return [{"id": "p_1", "pos": "WR", "rank": 1}]

    monkeypatch.setattr(main, "fetch_projections", fake_fetch)

    with pytest.raises(asyncio.CancelledError):
        await main._poll_loop()

    assert requested == [
        ("https://example.test/standard.json", "standard"),
        ("https://example.test/half-ppr.json", "half-ppr"),
        ("https://example.test/ppr.json", "ppr"),
    ]


async def test_successful_poll_populates_every_format_cache(monkeypatch, one_iteration):
    monkeypatch.setenv("PROJECTIONS_SNAPSHOT_URL", URL_TEMPLATE)

    async def fake_fetch(url, expect_format=None):
        return [
            {"id": "p_1", "name": "A", "pos": "WR", "rank": 1},
            {"id": "p_2", "name": "B", "pos": "RB", "rank": 2},
        ]

    monkeypatch.setattr(main, "fetch_projections", fake_fetch)

    with pytest.raises(asyncio.CancelledError):
        await main._poll_loop()

    for fmt in main.FORMATS:
        assert main._state[fmt]["projections"] == [
            {"id": "p_1", "name": "A", "pos": "WR", "rank": 1},
            {"id": "p_2", "name": "B", "pos": "RB", "rank": 2},
        ]
        assert main._state[fmt]["upstream_healthy"] is True
        assert main._state[fmt]["last_updated"] is not None


async def test_upstream_failure_marks_unhealthy(monkeypatch, one_iteration):
    monkeypatch.setenv("PROJECTIONS_SNAPSHOT_URL", URL_TEMPLATE)

    async def boom(url, expect_format=None):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(main, "fetch_projections", boom)

    with pytest.raises(asyncio.CancelledError):
        await main._poll_loop()

    for fmt in main.FORMATS:
        assert main._state[fmt]["upstream_healthy"] is False
        assert main._state[fmt]["projections"] == []


async def test_one_format_failing_does_not_affect_the_others(
    monkeypatch, one_iteration
):
    """Formats are polled independently — a broken half-ppr document must not
    mark standard and ppr unhealthy or discard their rows."""
    monkeypatch.setenv("PROJECTIONS_SNAPSHOT_URL", URL_TEMPLATE)

    async def selective(url, expect_format=None):
        if expect_format == "half-ppr":
            raise RuntimeError("that document is corrupt")
        return [{"id": "p_1", "pos": "WR", "rank": 1}]

    monkeypatch.setattr(main, "fetch_projections", selective)

    with pytest.raises(asyncio.CancelledError):
        await main._poll_loop()

    assert main._state["standard"]["upstream_healthy"] is True
    assert main._state["ppr"]["upstream_healthy"] is True
    assert main._state["standard"]["projections"] == [
        {"id": "p_1", "pos": "WR", "rank": 1}
    ]

    assert main._state["half-ppr"]["upstream_healthy"] is False
    assert main._state["half-ppr"]["projections"] == []


async def test_failure_after_success_retains_last_good_data(monkeypatch, one_iteration):
    """A later failure must not wipe the cache — stale data beats no data."""
    main._state["ppr"]["projections"] = [{"id": "p_1"}]
    main._state["ppr"]["upstream_healthy"] = True
    monkeypatch.setenv("PROJECTIONS_SNAPSHOT_URL", URL_TEMPLATE)

    async def boom(url, expect_format=None):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(main, "fetch_projections", boom)

    with pytest.raises(asyncio.CancelledError):
        await main._poll_loop()

    assert main._state["ppr"]["projections"] == [{"id": "p_1"}]
    assert main._state["ppr"]["upstream_healthy"] is False


async def test_poll_interval_read_from_env(monkeypatch):
    """POLL_INTERVAL_SECONDS controls the sleep duration."""
    monkeypatch.setenv("PROJECTIONS_SNAPSHOT_URL", URL_TEMPLATE)
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "42")
    slept = []

    async def capture(seconds):
        slept.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", capture)

    async def fake_fetch(url, expect_format=None):
        return []

    monkeypatch.setattr(main, "fetch_projections", fake_fetch)

    with pytest.raises(asyncio.CancelledError):
        await main._poll_loop()

    assert slept == [42]


def test_url_template_substitutes_the_format():
    assert (
        main._url_for("https://b.s3.amazonaws.com/{format}.json", "half-ppr")
        == "https://b.s3.amazonaws.com/half-ppr.json"
    )


def test_url_without_placeholder_is_used_verbatim():
    """A template missing `{format}` resolves to the same URL for all three.
    That is not silently accepted — `fetch_projections`'s expect_format check
    fails the two formats the document does not declare. See
    test_client.py::test_wrong_format_snapshot_is_rejected.
    """
    assert (
        main._url_for("https://b.s3.amazonaws.com/only.json", "ppr")
        == "https://b.s3.amazonaws.com/only.json"
    )


def test_now_iso_is_utc_and_parseable():
    from datetime import datetime

    value = main._now_iso()
    parsed = datetime.fromisoformat(value)

    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
