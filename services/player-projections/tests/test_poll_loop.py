import asyncio

import pytest

from player_projections import main


@pytest.fixture(autouse=True)
def reset_state():
    """_state is module-global; reset it around every test."""
    main._state["projections"] = []
    main._state["last_updated"] = None
    main._state["upstream_healthy"] = False
    yield
    main._state["projections"] = []
    main._state["last_updated"] = None
    main._state["upstream_healthy"] = False


@pytest.fixture
def one_iteration(monkeypatch):
    """Make the infinite poll loop run exactly one pass, then stop."""

    async def stop_after_first(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", stop_after_first)


async def test_stub_mode_returns_immediately_without_polling(monkeypatch):
    """No PLAYER_DATA_URL means the loop exits without ever calling upstream."""
    monkeypatch.setenv("PLAYER_DATA_URL", "")
    called = []
    monkeypatch.setattr(main, "fetch_projections", lambda url: called.append(url))

    await main._poll_loop()

    assert called == []
    assert main._state["upstream_healthy"] is False


async def test_successful_poll_populates_cache(monkeypatch, one_iteration):
    monkeypatch.setenv("PLAYER_DATA_URL", "https://example.test/ppr.json")

    async def fake_fetch(url):
        return [
            {"id": "p_1", "name": "A", "pos": "WR", "rank": 1},
            {"id": "p_2", "name": "B", "pos": "RB", "rank": 2},
        ]

    monkeypatch.setattr(main, "fetch_projections", fake_fetch)

    with pytest.raises(asyncio.CancelledError):
        await main._poll_loop()

    assert main._state["projections"] == [
        {"id": "p_1", "name": "A", "pos": "WR", "rank": 1},
        {"id": "p_2", "name": "B", "pos": "RB", "rank": 2},
    ]
    assert main._state["upstream_healthy"] is True
    assert main._state["last_updated"] is not None


async def test_upstream_failure_marks_unhealthy(monkeypatch, one_iteration):
    monkeypatch.setenv("PLAYER_DATA_URL", "https://example.test/ppr.json")

    async def boom(url):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(main, "fetch_projections", boom)

    with pytest.raises(asyncio.CancelledError):
        await main._poll_loop()

    assert main._state["upstream_healthy"] is False
    assert main._state["projections"] == []


async def test_failure_after_success_retains_last_good_data(monkeypatch, one_iteration):
    """A later failure must not wipe the cache — stale data beats no data."""
    main._state["projections"] = [{"id": "p_1"}]
    main._state["upstream_healthy"] = True
    monkeypatch.setenv("PLAYER_DATA_URL", "https://example.test/ppr.json")

    async def boom(url):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(main, "fetch_projections", boom)

    with pytest.raises(asyncio.CancelledError):
        await main._poll_loop()

    assert main._state["projections"] == [{"id": "p_1"}]
    assert main._state["upstream_healthy"] is False


async def test_poll_interval_read_from_env(monkeypatch):
    """POLL_INTERVAL_SECONDS controls the sleep duration."""
    monkeypatch.setenv("PLAYER_DATA_URL", "https://example.test/ppr.json")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "42")
    slept = []

    async def capture(seconds):
        slept.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", capture)

    async def fake_fetch(url):
        return []

    monkeypatch.setattr(main, "fetch_projections", fake_fetch)

    with pytest.raises(asyncio.CancelledError):
        await main._poll_loop()

    assert slept == [42]


def test_now_iso_is_utc_and_parseable():
    from datetime import datetime

    value = main._now_iso()
    parsed = datetime.fromisoformat(value)

    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
