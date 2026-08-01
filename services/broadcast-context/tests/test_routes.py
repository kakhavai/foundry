"""broadcast-context's five-route contract surface, plus auth.

The routes themselves are `collector_core.routes`' and are proved there against
a fake collector. What this file proves is that THIS service is wired to them —
that its descriptor reaches `/catalog`, that its own `signal_matches` is
consulted, and that auth is mounted.

There are deliberately **no** routes beyond the standard five: the spec says
so, and `as_of` is implemented as a filter rather than as a `/history` route
for exactly that reason.
"""

import time

import httpx
import respx
from collector_core.envelope import ENVELOPE_VERSION

from broadcast_context.adapters.upstream import UPSTREAM_URL
from broadcast_context.capture import SIGNAL_TYPES
from broadcast_context.signals import SUPPORTED_FILTERS

from .conftest import TEST_TOKEN, feed_document, week_rows


def wait_for_signals(client, *, count: int, timeout: float = 10.0) -> dict:
    """Poll `/signals` until a dispatched capture has landed.

    **`POST /refresh` returns 202 — accepted, not done.** The capture runs as a
    background task and lands in `CaptureState` whenever it finishes, so
    reading `/signals` on the next line is a race. It is a race that used to be
    won by accident, because nothing in the capture path yielded; the lake
    write now goes through `asyncio.to_thread`, so it genuinely suspends.

    Bounded and loud: on timeout this fails with what it actually saw rather
    than hanging or asserting against an empty envelope. Never replace it with
    a bare `client.get("/signals")` after a refresh.
    """
    deadline = time.monotonic() + timeout
    body = {"count": 0}
    while time.monotonic() < deadline:
        body = client.get("/signals").json()
        if body["count"] >= count:
            return body
        time.sleep(0.05)
    raise AssertionError(
        f"dispatched capture did not land within {timeout}s: "
        f"expected count >= {count}, last saw {body}"
    )


def _mock_feed(router, rows=None):
    router.get(UPSTREAM_URL).mock(
        return_value=httpx.Response(200, text=feed_document(rows or week_rows(1)))
    )


def test_health_is_open_and_ok(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_metrics_is_scrapeable(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]


def test_catalog_declares_this_collector(client):
    body = client.get("/catalog").json()
    assert body["collector"] == "broadcast-context"
    assert body["envelope_version"] == ENVELOPE_VERSION
    assert body["cadence_class"] == "weekly"
    assert set(body["signal_types"]) == set(SIGNAL_TYPES)
    assert "as_of" in body["filters"]


def test_signals_is_empty_before_any_capture(client):
    assert client.get("/signals").json() == {"envelopes": [], "count": 0}


def test_an_undeclared_filter_is_422_not_ignored(client):
    """A silently ignored filter returns everything and looks like it worked."""
    assert client.get("/signals?not_a_filter=x").status_code == 422


def test_an_unknown_signal_type_is_422(client):
    assert client.get("/signals?signal_type=nope").status_code == 422


def test_refresh_is_accepted_and_the_capture_eventually_lands(client):
    """202 means accepted. Observe it by polling — see `wait_for_signals`."""
    with respx.mock(assert_all_called=False) as router:
        _mock_feed(router)
        accepted = client.post("/refresh", json={"season": 2026, "week": 1})
        assert accepted.status_code == 202

        body = wait_for_signals(client, count=len(SIGNAL_TYPES))

    assert body["count"] == len(SIGNAL_TYPES)
    for envelope in body["envelopes"]:
        assert envelope["collector"] == "broadcast-context"
        assert envelope["coverage"]["expected"] >= envelope["coverage"]["present"]


def test_a_second_refresh_inside_the_floor_is_429(client):
    with respx.mock(assert_all_called=False) as router:
        _mock_feed(router)
        client.post("/refresh", json={"season": 2026, "week": 1})
        response = client.post("/refresh", json={"season": 2026, "week": 1})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_a_row_filter_narrows_the_signals(client):
    """`signal_matches` is this collector's own, so prove it is consulted."""
    with respx.mock(assert_all_called=False) as router:
        _mock_feed(router)
        client.post("/refresh", json={"season": 2026, "week": 1})
        wait_for_signals(client, count=len(SIGNAL_TYPES))

    body = client.get("/signals?window_id=snf").json()
    rows = [row for envelope in body["envelopes"] for row in envelope["signals"]]
    assert rows, "the filter matched nothing — is ROW_FILTERS wired up?"
    assert {row["window_id"] for row in rows} == {"snf"}


def test_as_of_reaches_the_route_and_withholds_the_future(client):
    """The guard, through the HTTP surface rather than the predicate alone.

    Everything in the capture was first observed at capture time, so an
    `as_of` before it must return an EMPTY signals array — not a 500, and not
    the full slate.
    """
    with respx.mock(assert_all_called=False) as router:
        _mock_feed(router)
        client.post("/refresh", json={"season": 2026, "week": 1})
        wait_for_signals(client, count=len(SIGNAL_TYPES))

    everything = client.get("/signals").json()
    assert everything["envelopes"][0]["signals"]

    withheld = client.get("/signals?as_of=2020-01-01T00:00:00Z").json()
    assert withheld["count"] == 1, "the envelope is still returned"
    assert withheld["envelopes"][0]["signals"] == []

    allowed = client.get("/signals?as_of=2099-01-01T00:00:00Z").json()
    assert len(allowed["envelopes"][0]["signals"]) == len(
        everything["envelopes"][0]["signals"]
    )


def test_a_malformed_as_of_is_422_through_the_route(client):
    with respx.mock(assert_all_called=False) as router:
        _mock_feed(router)
        client.post("/refresh", json={"season": 2026, "week": 1})
        wait_for_signals(client, count=len(SIGNAL_TYPES))

    assert client.get("/signals?as_of=yesterday").status_code == 422


def test_a_row_filter_no_longer_swallows_a_malformed_as_of(client):
    """**R3, through the HTTP surface.** This returned 200 with an empty list.

    The row filter matched nothing, `signal_matches` returned before the
    malformed instant was parsed, and the caller got a plausible-looking quiet
    week instead of the error CLAUDE.md's `pos=FLEX` reasoning exists to
    produce.
    """
    with respx.mock(assert_all_called=False) as router:
        _mock_feed(router)
        client.post("/refresh", json={"season": 2026, "week": 1})
        wait_for_signals(client, count=len(SIGNAL_TYPES))

    response = client.get("/signals?game_id=no-such-game&as_of=garbage")
    assert response.status_code == 422
    assert "as_of" in response.json()["detail"]


def test_the_two_structural_as_of_validation_holes_are_pinned(client):
    """Not a fix — a **disclosure with a test behind it**.

    `signal_matches` is per-row, so a query reaching no row reaches no
    validation. Two shapes therefore answer 200 with a malformed `as_of`, and
    neither is closable from this collector: an empty cache has no rows at
    all, and a `week` that does not match the envelope's scope is dropped by
    the shared router before any row is considered. Closing them needs a
    pre-filter seam in `collector-core`.

    Pinned rather than left unsaid, so the module docstring's table cannot
    quietly stop being true — and so this test fails loudly, asking to be
    updated, if that seam ever arrives.
    """
    # No capture has run: the cache is empty.
    assert client.get("/signals?as_of=garbage").status_code == 200

    with respx.mock(assert_all_called=False) as router:
        _mock_feed(router)
        client.post("/refresh", json={"season": 2026, "week": 1})
        wait_for_signals(client, count=len(SIGNAL_TYPES))

    # Rows exist, but the envelope's scope is week 1, so `?week=12` drops it.
    assert client.get("/signals?week=12&as_of=garbage").status_code == 200
    # ...while the same malformed value against the scope that DOES match is
    # the 422 above. Both halves, so this cannot pass by validating nothing.
    assert client.get("/signals?week=1&as_of=garbage").status_code == 422


def test_the_declared_filters_are_exactly_what_catalog_publishes(client):
    body = client.get("/catalog").json()
    assert tuple(body["filters"]) == SUPPORTED_FILTERS


def test_a_data_route_without_a_token_is_401(client):
    """Auth is enforced in-process, not only at the gateway: a ClusterIP is
    reachable by anything in the namespace, and smoke-test.sh port-forwards the
    Service directly."""
    assert client.get("/signals", headers={"Authorization": ""}).status_code == 401


def test_a_wrong_token_is_401(client):
    response = client.get(
        "/signals", headers={"Authorization": f"Bearer not-{TEST_TOKEN}"}
    )
    assert response.status_code == 401


def test_an_absent_token_env_fails_closed_with_503(client, monkeypatch):
    """Fails closed, loudly: a Secret that never syncs must not read as an open
    collector. The ArgoCD Application stays Healthy either way."""
    monkeypatch.setenv("COLLECTOR_TOKEN", "")
    assert client.get("/signals").status_code == 503
