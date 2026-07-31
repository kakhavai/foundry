"""The depth-chart adapter: parsing, grouping, freshness, and schema drift.

The schema here is the real nflverse one, verified against the live asset:
`dt,team,player_name,espn_id,gsis_id,pos_grp_id,pos_grp,pos_id,pos_name,
pos_abb,pos_slot,pos_rank`. It carries no season or week column — it is a
current snapshot stamped with `dt`.
"""

from datetime import UTC, datetime

import httpx
import pytest
import respx

from roster_scope.adapters.depth_chart import (
    DepthChartSchemaError,
    DepthChartTooLarge,
    depth_chart_url,
    fetch_depth_charts,
    parse_depth_chart_csv,
)

from .conftest import DEPTH_HEADER, depth_csv, depth_row

FETCHED_AT = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def parse(rows, *, header=DEPTH_HEADER):
    return parse_depth_chart_csv(depth_csv(rows, header), fetched_at=FETCHED_AT)


def test_groups_rows_by_canonical_team():
    charts = parse(
        [
            depth_row("KC", "QB", 1, "Patrick Mahomes"),
            depth_row("JAC", "QB", 1, "Trevor Lawrence"),
        ]
    )
    assert set(charts) == {"KC", "JAX"}
    assert charts["JAX"].rows[0].name_raw == "Trevor Lawrence"


def test_unknown_team_is_dropped_rather_than_passed_through():
    """Its slots then read as missing, which is the correct accounting for
    'we have no chart for this team'."""
    assert parse([depth_row("XYZ", "QB", 1, "Nobody")]) == {}


def test_unranked_or_nameless_rows_are_dropped():
    charts = parse(
        [
            depth_row("KC", "QB", 1, "Real Player"),
            "2026-09-15T09:00:00Z,KC,No Rank,1,x,1,G,1,N,QB,1,",
            "2026-09-15T09:00:00Z,KC,,1,x,1,G,1,N,QB,2,2",
        ]
    )
    assert [r.name_raw for r in charts["KC"].rows] == ["Real Player"]


def test_rows_outside_every_rules_demand_are_dropped_at_ingest():
    """A memory decision, not a modelling one -- and gated on rule demand,
    not on recognition.

    `LDE` and `ATH` are labels `canonical_position` does not recognise at all
    (`None`) -- special-teams and gadget-player codes the feed carries that
    neither rule set has ever wanted. That is currently the *only* way a row
    is dropped here: the player scope (QB/RB/WR/TE/K/DST) and the matchup
    scope (CB/S/LB/DL/OL) together happen to cover every canonical group
    `POSITION_ALIASES` can produce, so "recognised" and "wanted" currently
    coincide again -- the same coincidence that broke once already (see
    `WANTED_POSITIONS`'s comment) when only the player scope existed. Gating
    on `position not in WANTED_POSITIONS` rather than on `position is None`
    is what keeps this test's assertion correct independent of that
    coincidence: if a third rule set ever wants a not-yet-covered canonical
    group, or if a scope's rules shrink, this filter still reflects rule
    demand rather than silently drifting back to "whatever is recognised".
    `test_matchup_positions_are_retained_at_ingest` below is the flip side:
    it pins that a *recognised and wanted* row (`LT` -> `OL`) is retained.
    """
    charts = parse(
        [
            depth_row("KC", "QB", 1, "A Passer"),
            depth_row("KC", "LDE", 1, "An End"),
            depth_row("KC", "ATH", 1, "A Gadget Player"),
        ]
    )
    assert {r.position_raw for r in charts["KC"].rows} == {"QB"}


def test_matchup_positions_are_retained_at_ingest():
    """Task 6 builds the matchup list (CB/S/LB/DL/OL) from this same feed, so
    those rows must survive Filter 1 even though the player scope's own
    rules never consult them. `LT` canonicalizes to `OL` -- our own line,
    per `MATCHUP_RULES` -- so it is retained, not dropped, once
    `WANTED_POSITIONS` includes the matchup scope. Without this test,
    someone restoring the pre-matchup-scope filter (gating on the player
    scope's positions alone) would silently break Task 6's data source, and
    nothing here would catch it.
    """
    charts = parse(
        [
            depth_row("KC", "QB", 1, "A Passer"),
            depth_row("KC", "LT", 1, "A Tackle"),
        ]
    )
    assert {r.position_raw for r in charts["KC"].rows} == {"QB", "LT"}


def test_ingest_gates_on_configured_demand_not_on_recognition(monkeypatch):
    """Synthetic, and here on purpose: no *real* position can currently prove
    this gate matters.

    The player scope (QB/RB/WR/TE/K/DST) and the matchup scope
    (CB/S/LB/DL/OL) together already cover every canonical group
    `canonical_position` can produce, so today `position not in
    WANTED_POSITIONS` and `position is None` compute the exact same function
    -- there is no recognised-but-unwanted label left to tell them apart.
    That coverage gap is not a future risk to watch for; it has *already*
    happened, silently, as a side effect of Task 5 adding `OL` to
    `WANTED_POSITIONS`. Without a synthetic case, an accidental revert to
    `is None` -- which is exactly the change that reopened the OOM-shaped
    hole once already -- would ship with the full suite green.

    So the test manufactures the gap: monkeypatch `WANTED_POSITIONS` down to
    a set that deliberately excludes `OL`, then feed a row that
    `canonical_position` recognises fine (`LT` -> `OL`) but this narrowed
    set does not want. Under the real gate that row is dropped. Under
    `is None` it would be retained regardless of `WANTED_POSITIONS`, because
    that mutation never looks at the set at all -- which is precisely what
    this test is here to catch.
    """
    monkeypatch.setattr(
        "roster_scope.adapters.depth_chart.WANTED_POSITIONS",
        frozenset({"QB", "RB", "WR", "TE", "K", "DST", "CB", "S", "LB", "DL"}),
    )
    charts = parse(
        [
            depth_row("KC", "QB", 1, "A Passer"),
            depth_row("KC", "LT", 1, "A Tackle"),
        ]
    )
    assert {r.position_raw for r in charts["KC"].rows} == {"QB"}


def test_only_the_newest_snapshot_of_a_slot_is_kept():
    """The feed is a time series: the same slot appears once per `dt`, going
    back over the season. Keeping every one is what made the retained rows
    unbounded."""
    charts = parse(
        [
            depth_row("KC", "QB", 1, "Old Starter", dt="2026-08-01T09:00:00Z"),
            depth_row("KC", "QB", 1, "New Starter", dt="2026-09-15T09:00:00Z"),
            depth_row("KC", "QB", 1, "Older Starter", dt="2026-07-01T09:00:00Z"),
        ]
    )
    assert [r.name_raw for r in charts["KC"].rows] == ["New Starter"]
    assert charts["KC"].captured_at == datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def test_retained_rows_are_bounded_by_config_not_by_feed_size():
    """The property that actually prevents the OOM: a feed with a season of
    daily snapshots collapses to one row per slot."""
    rows = []
    for day in range(1, 29):
        for rank in range(1, 5):
            rows.append(
                depth_row(
                    "KC",
                    "WR",
                    rank,
                    f"Receiver {rank}",
                    dt=f"2026-09-{day:02d}T09:00:00Z",
                )
            )
    charts = parse(rows)
    assert len(charts["KC"].rows) == 4, (
        f"112 input rows collapsed to {len(charts['KC'].rows)} — retention is "
        f"tracking the feed rather than the config"
    )


def test_jersey_number_is_absent_not_guessed():
    """The feed carries no jersey column, so the ref simply has one fewer
    attribute for the resolver to match on."""
    charts = parse([depth_row("KC", "QB", 1, "A Player")])
    assert charts["KC"].rows[0].jersey_number is None


def test_captured_at_is_the_newest_dt_for_that_team():
    charts = parse(
        [
            depth_row("KC", "QB", 1, "A", dt="2026-09-14T08:00:00Z"),
            depth_row("KC", "QB", 2, "B", dt="2026-09-15T09:30:00Z"),
            depth_row("BUF", "QB", 1, "C", dt="2026-09-01T00:00:00Z"),
        ]
    )
    assert charts["KC"].captured_at == datetime(2026, 9, 15, 9, 30, tzinfo=UTC)
    # Per team, not one number for the fetch — one frozen chart must stay
    # visible rather than being averaged away.
    assert charts["BUF"].captured_at == datetime(2026, 9, 1, tzinfo=UTC)


def test_captured_at_falls_back_to_the_fetch_instant():
    """A row without a usable `dt` is as fresh as the fetch. Inventing an old
    timestamp would make every team permanently stale."""
    charts = parse([depth_row("KC", "QB", 1, "A", dt="")])
    assert charts["KC"].captured_at == FETCHED_AT


def test_unparseable_dt_falls_back_rather_than_raising():
    charts = parse([depth_row("KC", "QB", 1, "A", dt="last tuesday")])
    assert charts["KC"].captured_at == FETCHED_AT


def test_naive_dt_is_read_as_utc():
    charts = parse([depth_row("KC", "QB", 1, "A", dt="2026-09-14T08:00:00")])
    assert charts["KC"].captured_at == datetime(2026, 9, 14, 8, 0, tzinfo=UTC)


def test_missing_required_column_fails_loudly():
    """Schema drift must fail the capture with a classified reason, not map
    nulls into an append-only lake."""
    header = "dt,team,player_name,pos_abb"  # no pos_rank
    with pytest.raises(DepthChartSchemaError) as exc:
        parse(["2026-09-15T09:00:00Z,KC,A Player,QB"], header=header)
    assert "pos_rank" in str(exc.value)


def test_an_empty_document_is_a_schema_error_not_an_empty_chart():
    """Zero teams would otherwise read as a successful capture of nothing —
    every slot missing, but with no error explaining why."""
    with pytest.raises(DepthChartSchemaError):
        parse_depth_chart_csv("", fetched_at=FETCHED_AT)


def test_schema_errors_are_value_errors_so_they_classify_as_malformed():
    """`CollectorMetrics.reason_for` maps ValueError to `malformed`; if either
    stopped being a ValueError the reason label would silently become
    `unknown`."""
    assert issubclass(DepthChartSchemaError, ValueError)
    assert issubclass(DepthChartTooLarge, ValueError)


def test_depth_chart_url_substitutes_the_season(monkeypatch):
    monkeypatch.setattr(
        "roster_scope.adapters.depth_chart.DEPTH_CHART_URL",
        "https://feed.test/{season}.csv",
    )
    assert depth_chart_url(2027) == "https://feed.test/2027.csv"


@pytest.fixture
def feed_url(monkeypatch):
    monkeypatch.setattr(
        "roster_scope.adapters.depth_chart.DEPTH_CHART_URL",
        "https://feed.test/{season}.csv",
    )
    return "https://feed.test/2026.csv"


@respx.mock
async def test_fetch_streams_and_parses(feed_url):
    respx.get(feed_url).mock(
        return_value=httpx.Response(
            200, text=depth_csv([depth_row("KC", "QB", 1, "Patrick Mahomes")])
        )
    )
    async with httpx.AsyncClient() as client:
        charts = await fetch_depth_charts(2026, 1, client, now=FETCHED_AT)
    assert charts["KC"].rows[0].name_raw == "Patrick Mahomes"


@respx.mock
async def test_fetch_stitches_lines_across_chunk_boundaries(feed_url):
    """The streaming reader rejoins partial lines. A row lost or duplicated at
    a chunk boundary would silently change one team's depth order."""
    rows = [depth_row("KC", "WR", n, f"Receiver {n}") for n in range(1, 40)]
    respx.get(feed_url).mock(return_value=httpx.Response(200, text=depth_csv(rows)))
    async with httpx.AsyncClient() as client:
        charts = await fetch_depth_charts(2026, 1, client, now=FETCHED_AT)
    assert len(charts["KC"].rows) == 39
    assert [r.depth_order for r in charts["KC"].rows] == list(range(1, 40))


@respx.mock
async def test_fetch_does_not_buffer_the_whole_body(feed_url):
    """The regression test for the OOM that killed this collector's first
    deploy.

    The real asset is ~37 MB and the old `resp.text` path held it three times
    over — httpx's bytes, the decoded str, and the StringIO copy — which
    OOM-killed a 256Mi pod (exit 137, CrashLoopBackOff). Asserts the
    mechanism: `aiter_text` is what makes peak memory independent of document
    size, and it is exactly what `resp.text` does not do.
    """
    seen = {"streamed": False, "buffered": False}
    body = depth_csv([depth_row("KC", "QB", n, f"P{n}") for n in range(1, 500)])
    respx.get(feed_url).mock(return_value=httpx.Response(200, text=body))

    real_aiter_text = httpx.Response.aiter_text
    real_text = httpx.Response.text

    async def spy_aiter_text(self, *args, **kwargs):
        seen["streamed"] = True
        async for chunk in real_aiter_text(self, *args, **kwargs):
            yield chunk

    @property
    def spy_text(self):
        seen["buffered"] = True
        return real_text.fget(self)

    httpx.Response.aiter_text = spy_aiter_text
    httpx.Response.text = spy_text
    try:
        async with httpx.AsyncClient() as client:
            charts = await fetch_depth_charts(2026, 1, client, now=FETCHED_AT)
    finally:
        httpx.Response.aiter_text = real_aiter_text
        httpx.Response.text = real_text

    assert seen["streamed"], (
        "the adapter did not stream the response — a 37 MB upstream will OOM "
        "the pod again"
    )
    assert not seen["buffered"], (
        "the adapter touched Response.text, which materializes the whole body "
        "— this is the exact call that OOM-killed the pod"
    )
    assert len(charts["KC"].rows) == 499


@respx.mock
async def test_fetch_refuses_an_unbounded_feed(feed_url, monkeypatch):
    monkeypatch.setattr("roster_scope.adapters.depth_chart.MAX_DEPTH_CHART_CHARS", 200)
    body = depth_csv([depth_row("KC", "QB", n, f"P{n}") for n in range(1, 200)])
    respx.get(feed_url).mock(return_value=httpx.Response(200, text=body))
    async with httpx.AsyncClient() as client:
        with pytest.raises(DepthChartTooLarge):
            await fetch_depth_charts(2026, 1, client, now=FETCHED_AT)


@respx.mock
async def test_fetch_raises_on_an_error_status(feed_url):
    respx.get(feed_url).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_depth_charts(2026, 1, client, now=FETCHED_AT)
