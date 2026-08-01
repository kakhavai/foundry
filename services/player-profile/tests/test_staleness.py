"""The spec's named failure mode: a listed value that silently goes stale.

> a listed weight or position that silently goes stale is invisible ... keeps
> producing well-formed records with the old value, and
> `position_age_curve_stage` then places them on the wrong curve entirely.
> Nothing errors.

The guard is a per-field `last_changed_at` plus a 45-day re-confirmation
assertion, and the spec's own condition on it is the hard part: **staleness must
be a measured age, not an assumption.** These tests are what stop the guard
degenerating into a field that always says "confirmed just now" — which is what
it becomes the moment `last_confirmed_at` advances on a pass that read a cached
copy rather than a re-served document.
"""

from datetime import timedelta

import httpx
import respx

from player_profile.adapters import upstream as upstream_mod
from player_profile.capture import BIOGRAPHICAL, capture_player_profile
from player_profile.staleness import (
    STALE_AFTER_DAYS,
    TRACKED_FIELDS,
    FieldHistory,
    advance,
    in_season,
    is_stale,
    rfc3339,
)

from .conftest import (
    FIXTURE_PLAYERS,
    NOW,
    SEASON,
    WEEK,
    mock_identity,
    mock_upstreams,
    players_csv,
)


async def capture(lake, *, now=NOW, week=WEEK, **kwargs):
    async with httpx.AsyncClient() as client:
        return await capture_player_profile(
            SEASON, week, client=client, lake=lake, now=now, **kwargs
        )


# ── the two timestamps, in isolation ─────────────────────────────────────────


def test_the_spec_names_exactly_these_two_fields():
    assert TRACKED_FIELDS == ("position", "weight_lbs")
    assert STALE_AFTER_DAYS == 45


def test_a_first_observation_claims_no_change_it_did_not_see():
    """`last_changed_at` is null on a first observation rather than `now`.

    Stamping `now` would claim a change this collector never observed, and the
    field would then be indistinguishable from a genuine offseason weight
    change on the very next read.
    """
    history = advance(None, "RB", now=NOW, republished=True)

    assert history.last_changed_at is None
    assert history.last_confirmed_at == rfc3339(NOW)


def test_a_changed_value_advances_both_stamps():
    prior = FieldHistory(
        value="TE", last_changed_at="2026-01-01T00:00:00Z", last_confirmed_at=None
    )

    history = advance(prior, "RB", now=NOW, republished=True)

    assert history.value == "RB"
    assert history.last_changed_at == rfc3339(NOW)
    assert history.last_confirmed_at == rfc3339(NOW)


def test_an_unchanged_value_on_a_republished_document_is_re_confirmed():
    """A value that never changes must not read as stale forever — the document
    asserting it again IS the re-confirmation."""
    prior = FieldHistory(
        value="RB",
        last_changed_at="2026-01-01T00:00:00Z",
        last_confirmed_at="2026-01-01T00:00:00Z",
    )

    history = advance(prior, "RB", now=NOW, republished=True)

    assert history.last_changed_at == "2026-01-01T00:00:00Z"
    assert history.last_confirmed_at == rfc3339(NOW)


def test_a_304_re_confirms_nothing():
    """**This is the load-bearing case.** A `304` means nobody republished the
    listed value, so reading the cached copy back must not reset the clock.

    Delete this behaviour and the guard still exists, still passes every other
    test, and can never fire: every pass would look like a re-confirmation and
    the measured age would never exceed one capture interval.
    """
    prior = FieldHistory(
        value="RB",
        last_changed_at="2026-01-01T00:00:00Z",
        last_confirmed_at="2026-01-01T00:00:00Z",
    )

    history = advance(prior, "RB", now=NOW, republished=False)

    assert history.last_confirmed_at == "2026-01-01T00:00:00Z"
    assert history.last_changed_at == "2026-01-01T00:00:00Z"


# ── the assertion ────────────────────────────────────────────────────────────


def test_a_value_re_confirmed_inside_the_window_is_not_stale():
    history = FieldHistory(
        value="RB",
        last_changed_at=None,
        last_confirmed_at=rfc3339(NOW - timedelta(days=STALE_AFTER_DAYS - 1)),
    )

    assert is_stale(history, now=NOW, week=WEEK) is False


def test_a_value_not_re_confirmed_past_the_window_is_stale():
    history = FieldHistory(
        value="RB",
        last_changed_at=None,
        last_confirmed_at=rfc3339(NOW - timedelta(days=STALE_AFTER_DAYS + 1)),
    )

    assert is_stale(history, now=NOW, week=WEEK) is True


def test_the_boundary_is_strictly_greater_than():
    """Exactly 45 days is not yet past 45 days. Pinned because an off-by-one
    here silently shifts every alert by a day."""
    at_the_boundary = FieldHistory(
        value="RB",
        last_changed_at=None,
        last_confirmed_at=rfc3339(NOW - timedelta(days=STALE_AFTER_DAYS)),
    )

    assert is_stale(at_the_boundary, now=NOW, week=WEEK) is False


def test_an_unknown_age_is_not_asserted_stale():
    """ "Staleness must be a measured age, not an assumption."

    A value never seen confirmed has an UNKNOWN age, not an infinite one.
    Treating unknown as stale would fire the assertion for the entire scope on
    the first pass of every new deploy, which is the fastest known way to get an
    alert switched off.
    """
    never_confirmed = FieldHistory(
        value="RB", last_changed_at=None, last_confirmed_at=None
    )

    assert is_stale(never_confirmed, now=NOW, week=WEEK) is False


def test_the_assertion_is_scoped_to_the_season():
    """The spec says "during the season". A 45-day-old listed weight in June is
    ordinary; the league is not playing."""
    stale = FieldHistory(
        value="RB",
        last_changed_at=None,
        last_confirmed_at=rfc3339(NOW - timedelta(days=STALE_AFTER_DAYS + 10)),
    )

    assert is_stale(stale, now=NOW, week=WEEK) is True
    assert is_stale(stale, now=NOW, week=0) is False
    assert in_season(WEEK) is True
    assert in_season(0) is False


def test_an_unparseable_stored_timestamp_reads_as_unknown_not_as_a_crash():
    """The value came out of an append-only lake object that may predate a
    format change. One unparseable cell must not fail a whole pass."""
    history = FieldHistory(
        value="RB", last_changed_at=None, last_confirmed_at="not-a-timestamp"
    )

    assert is_stale(history, now=NOW, week=WEEK) is False


# ── end to end, through the capture ──────────────────────────────────────────


@respx.mock
async def test_a_capture_publishes_both_stamps_for_both_tracked_fields(lake):
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    rows = envelopes[BIOGRAPHICAL].signals
    assert rows
    for row in rows:
        assert row["position_last_confirmed_at"] == rfc3339(NOW)
        assert row["weight_lbs_last_confirmed_at"] == rfc3339(NOW)
        assert row["position_last_changed_at"] is None
        assert row["position_stale"] is False


@respx.mock
async def test_a_position_change_between_passes_is_dated(lake):
    """The spec's own example: a tight end converted to fullback. The record
    stays well-formed either way — the date is the only thing that says it
    moved."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    await capture(lake)

    # Bravo moves RB -> WR, and the document is genuinely re-served.
    moved = [list(record) for record in FIXTURE_PLAYERS]
    moved[1][3] = "WR"
    respx.mock.get(upstream_mod.PLAYERS_URL).respond(200, text=players_csv(moved))

    later = NOW + timedelta(days=2)
    envelopes = await capture(lake, now=later)

    bravo = next(
        row
        for row in envelopes[BIOGRAPHICAL].signals
        if row["player_id"] == "fdy-bbbb11112222"
    )
    assert bravo["position"] == "WR"
    assert bravo["position_last_changed_at"] == rfc3339(later)


@respx.mock
async def test_an_unchanged_upstream_lets_the_measured_age_grow_and_fires(lake):
    """The end-to-end version of the guard, and the reason conditional GET is
    not merely a cost control here.

    Pass one reads the document. Pass two, 60 days later, is answered `304` —
    so nothing re-confirmed the listed position, the measured age crosses 45
    days, and the assertion fires. A collector that re-confirmed on the cached
    read would report every one of these players as freshly verified.
    """
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    await capture(lake)

    # The profile document answers 304 from here on: byte-identical, nothing
    # republished, so nothing about a listed position was re-asserted.
    respx.mock.get(upstream_mod.PLAYERS_URL).respond(304)

    later = NOW + timedelta(days=60)
    envelopes = await capture(lake, now=later)

    rows = envelopes[BIOGRAPHICAL].signals
    assert rows, "the 304 discarded the capture instead of using the memo"
    assert all(row["position_stale"] is True for row in rows)
    assert len(rows) > 1
    reasons = {error["reason"] for error in envelopes[BIOGRAPHICAL].errors}
    assert "position_not_reconfirmed" in reasons, reasons


@respx.mock
async def test_an_unreadable_lake_costs_the_history_not_the_capture(lake):
    """The profile rows do not depend on the history, so a lake that cannot be
    read must degrade the guard to "unknown" rather than fail a pass that is
    otherwise fine — and it must SAY so, or an operator reads the resulting
    nulls as a first deploy."""
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)

    # Only this collector's own prefix fails. Breaking every read would take
    # the scope out first and the pass would fail closed for a different
    # reason, proving nothing about the history path.
    original = lake.list_keys

    def explode(collector, *args, **kwargs):
        if collector == "player-profile":
            raise RuntimeError("lake unreachable")
        return original(collector, *args, **kwargs)

    lake.list_keys = explode

    envelopes = await capture(lake)

    assert envelopes[BIOGRAPHICAL].signals
    reasons = {error["reason"] for error in envelopes[BIOGRAPHICAL].errors}
    assert "prior_history_unavailable" in reasons, reasons


@respx.mock
async def test_a_stale_position_does_not_dent_coverage(lake):
    """Why the metric in `test_metrics.py` exists at all.

    `collector_coverage_ratio` cannot see this failure mode: a player on a
    two-month-old position still produces a complete, fully-covered record.
    Asserting that here is what justifies a separate gauge rather than an alert
    on coverage.
    """
    mock_upstreams(respx.mock)
    mock_identity(respx.mock)
    fresh = await capture(lake)
    respx.mock.get(upstream_mod.PLAYERS_URL).respond(304)

    stale = await capture(lake, now=NOW + timedelta(days=60))

    assert all(row["position_stale"] for row in stale[BIOGRAPHICAL].signals)
    assert stale[BIOGRAPHICAL].coverage.ratio == fresh[BIOGRAPHICAL].coverage.ratio
    assert stale[BIOGRAPHICAL].coverage.present == fresh[BIOGRAPHICAL].coverage.present
