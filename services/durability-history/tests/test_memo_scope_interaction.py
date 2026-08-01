"""The per-season memos across a SCOPE CHANGE — where the Critical lived.

This collector pushes the scope filter into the parse, which is what keeps the
8.28 MB weekly-stats file from materialising ~4,600 unwanted players. The cost of
that choice is that a per-season memo is only ever complete *for the scope that
built it* — unlike `player-profile`, which memoises the unfiltered table and
narrows afterwards, so a season-keyed memo is complete there by construction.

Three facts combine into the failure:

* prior-season files are immutable and `304` forever (this collector's own
  adapter docstring says so);
* `roster-scope` membership changes weekly;
* a `304` serves the memo.

So a player who enters the scope after process start would be served a memo built
without him, publish a history containing only the current season, and report
`career_history_complete: true` over it. A believable availability rate that is
simply wrong, under a flag that promises it is not — the exact failure the
collector exists to prevent, and the one that made the bounded three-season
window acceptable in the first place.

Nothing else in the suite exercises the memos across a scope change, which is
precisely where this lives.
"""

import httpx
import respx

from durability_history.adapters import upstream as upstream_mod
from durability_history.capture import (
    DURABILITY_PROFILE,
    INJURY_HISTORY,
    capture_durability_history,
    reset_published_digests,
)

from .conftest import (
    ALPHA,
    BRAVO,
    CANONICAL_IDS,
    CHARLIE,
    FIXTURE_PLAYERS,
    HISTORY_SEASONS,
    NOW,
    SEASON,
    WEEK,
    SpyLake,
    games_csv,
    injuries_csv,
    mock_identity,
    players_csv,
    scope_envelope,
    snap_counts_csv,
    stats_csv,
)

# The pfr keys the snap feed is keyed by, for the two players the superset tests
# use. Taken from FIXTURE_PLAYERS rather than retyped, so a fixture edit cannot
# leave these silently naming nobody.
BRAVO_PFR = next(r[1] for r in FIXTURE_PLAYERS if r[0] == BRAVO)
CHARLIE_PFR = next(r[1] for r in FIXTURE_PLAYERS if r[0] == CHARLIE)

CURRENT_SEASON = HISTORY_SEASONS[-1]
PRIOR_SEASONS = HISTORY_SEASONS[:-1]


async def capture(lake):
    async with httpx.AsyncClient() as client:
        return await capture_durability_history(
            SEASON, WEEK, client=client, lake=lake, now=NOW
        )


def _mock_immutable_prior_seasons(mock, *, etag: str = '"v1"'):
    """The real cadence: prior seasons carry an ETag and 304 on the second ask;
    the current season is republished every week and always answers 200.

    Returns the per-season routes so a test can count the re-reads.
    """
    mock.get(upstream_mod.GAMES_URL).respond(200, text=games_csv())
    mock.get(upstream_mod.PLAYERS_URL).respond(200, text=players_csv())

    routes = {}
    for season in HISTORY_SEASONS:
        for url, body in (
            (upstream_mod.INJURIES_URL.format(season=season), injuries_csv(season)),
            (
                upstream_mod.SNAP_COUNTS_URL.format(season=season),
                snap_counts_csv(season),
            ),
            (upstream_mod.STATS_URL.format(season=season), stats_csv(season)),
        ):
            route = mock.get(url)
            if season == CURRENT_SEASON:
                route.respond(200, text=body, headers={"ETag": etag})
            else:

                def handler(request, _body=body, _etag=etag):
                    if request.headers.get("if-none-match") == _etag:
                        return httpx.Response(304)
                    return httpx.Response(200, text=_body, headers={"ETag": _etag})

                route.mock(side_effect=handler)
            routes[url] = route
    return routes


@respx.mock
async def test_a_player_entering_the_scope_gets_his_FULL_prior_history():
    """The Critical, end to end.

    Pass 1 narrows without Bravo. Pass 2 adds him — and the two prior seasons
    `304`, because they are immutable. Without the keep-set on the memo he is
    served a table built without him and publishes only 2026: six games possible
    instead of eighteen, one injury instead of three, both hamstrings and the
    recurrence gone, `availability_rate` 0.8333 — and
    `career_history_complete: true` over the top of it.
    """
    _mock_immutable_prior_seasons(respx.mock)
    mock_identity(respx.mock)

    without_bravo = SpyLake()
    without_bravo.write(
        scope_envelope(player_ids=[CANONICAL_IDS[ALPHA]], include_team_defenses=False)
    )
    await capture(without_bravo)

    with_bravo = SpyLake()
    with_bravo.write(
        scope_envelope(
            player_ids=[CANONICAL_IDS[ALPHA], CANONICAL_IDS[BRAVO]],
            include_team_defenses=False,
        )
    )
    envelopes = await capture(with_bravo)

    profile = next(
        row
        for row in envelopes[DURABILITY_PROFILE].signals
        if row["player_id"] == CANONICAL_IDS[BRAVO]
    )
    assert profile["career_games_possible"] == 18, (
        "a player who entered the scope after process start was served a memo "
        "built without him — his prior seasons vanished"
    )
    assert profile["career_games_missed_injury"] == 3
    assert profile["sample_size_events"] == 3

    history = next(
        row
        for row in envelopes[INJURY_HISTORY].signals
        if row["player_id"] == CANONICAL_IDS[BRAVO]
    )
    sites = [event["injury_site"] for event in history["injury_events"]]
    assert sites == ["hamstring", "hamstring", "knee"], sites
    assert any(e["is_recurrence_of"] for e in history["injury_events"]), (
        "the recurrence link is gone, so both hamstrings were not reconstructed"
    )


@respx.mock
async def test_entering_the_scope_produces_the_SAME_ROW_as_having_been_there():
    """The strongest form of the Critical, and the one that covers all THREE
    per-season feeds at once.

    Asserting a handful of fields catches the injury memo and misses the stats
    one, because `post_return_production_delta` is the only thing the weekly-stats
    feed backs — mutation testing showed exactly that: reverting only the stats
    memo to season-keyed left every earlier assertion green. Comparing the whole
    published row against a control that had the player in scope from the first
    pass is what closes it: any feed whose memo was served for the wrong scope
    changes some field.
    """
    _mock_immutable_prior_seasons(respx.mock)
    mock_identity(respx.mock)

    def wide_lake():
        lake = SpyLake()
        lake.write(
            scope_envelope(
                player_ids=[CANONICAL_IDS[ALPHA], CANONICAL_IDS[BRAVO]],
                include_team_defenses=False,
            )
        )
        return lake

    def bravo_rows(envelopes):
        return {
            signal_type: next(
                row
                for row in envelope.signals
                if row["player_id"] == CANONICAL_IDS[BRAVO]
            )
            for signal_type, envelope in envelopes.items()
        }

    # Control: a process that has had Bravo in scope since its first pass.
    control = bravo_rows(await capture(wide_lake()))

    # The real sequence: narrow first, widen second, prior seasons 304 in between.
    upstream_mod.reset_upstream_memo()
    reset_published_digests()
    narrow = SpyLake()
    narrow.write(
        scope_envelope(player_ids=[CANONICAL_IDS[ALPHA]], include_team_defenses=False)
    )
    await capture(narrow)
    entered = bravo_rows(await capture(wide_lake()))

    assert (
        control["player_return_trajectory"]["post_return_production_delta"] is not None
    ), (
        "the control produced no production delta, so this test cannot see the "
        "weekly-stats memo at all"
    )
    for signal_type in control:
        assert entered[signal_type] == control[signal_type], (
            f"{signal_type}: a player who entered the scope after process start "
            "published a different row from one who had always been in it"
        )


@respx.mock
async def test_a_widened_scope_re_reads_the_prior_seasons_it_must():
    """The self-heal, made visible. `_read_rows` already drops a stored ETag and
    re-reads unconditionally when the memo is absent; treating a non-covering
    memo AS absent is what routes the scope change into that path."""
    routes = _mock_immutable_prior_seasons(respx.mock)
    mock_identity(respx.mock)
    prior = [
        routes[upstream_mod.INJURIES_URL.format(season=season)]
        for season in PRIOR_SEASONS
    ]

    lake = SpyLake()
    lake.write(
        scope_envelope(player_ids=[CANONICAL_IDS[ALPHA]], include_team_defenses=False)
    )
    await capture(lake)
    after_first = [route.call_count for route in prior]
    assert all(count == 1 for count in after_first), after_first

    widened = SpyLake()
    widened.write(
        scope_envelope(
            player_ids=[CANONICAL_IDS[ALPHA], CANONICAL_IDS[BRAVO]],
            include_team_defenses=False,
        )
    )
    await capture(widened)

    # Two requests each: the conditional one that 304s, then the unconditional
    # re-read the dropped ETag forces.
    assert [route.call_count for route in prior] == [3, 3], (
        "the widened scope did not re-read the immutable prior seasons"
    )


@respx.mock
async def test_a_NARROWED_scope_costs_a_304_rather_than_a_re_read():
    """The other direction, and the reason the stored keep-set is a UNION rather
    than a replacement: a memo built for a superset already holds everything a
    smaller scope asks for, so shrinking must not cost a re-download. Without the
    union the two scopes would ping-pong, each invalidating the other's memo
    every week — 34 MB, weekly, forever.

    Measured in BYTES, not calls: conditional GET always makes the round trip.
    What distinguishes the two cases is that a covered memo ends after the `304`,
    while a non-covering one drops the ETag and pulls the whole document again —
    which is what `test_a_widened_scope_re_reads_the_prior_seasons_it_must` sees
    as a third request.
    """
    routes = _mock_immutable_prior_seasons(respx.mock)
    mock_identity(respx.mock)
    # EVERY per-season feed, not just one. The union lives in three separate
    # readers, and a test that watched only `stats_player_week` would leave the
    # other two free to replace their keep-set instead — mutation testing found
    # exactly that.
    prior = [
        routes[template.format(season=season)]
        for template in (
            upstream_mod.INJURIES_URL,
            upstream_mod.SNAP_COUNTS_URL,
            upstream_mod.STATS_URL,
        )
        for season in PRIOR_SEASONS
    ]
    assert len(prior) == 3 * len(PRIOR_SEASONS)

    wide = SpyLake()
    wide.write(
        scope_envelope(
            player_ids=[CANONICAL_IDS[ALPHA], CANONICAL_IDS[BRAVO]],
            include_team_defenses=False,
        )
    )
    await capture(wide)

    narrow = SpyLake()
    narrow.write(
        scope_envelope(player_ids=[CANONICAL_IDS[ALPHA]], include_team_defenses=False)
    )
    await capture(narrow)

    for route in prior:
        assert route.call_count == 2, (route.call_count, route)
        second = route.calls[1]
        assert second.request.headers.get("if-none-match") == '"v1"', (
            "the second pass did not send a conditional request"
        )
        assert second.response.status_code == 304, (
            "a narrowed scope re-downloaded a memo that already covered it"
        )


@respx.mock
async def test_a_memo_serving_a_SUPERSET_does_not_leak_out_of_scope_players():
    """The union means the memo legitimately holds rows for players who are not
    in this pass's scope. They must be filtered on EMIT as well as on parse, or
    the narrowing quietly stops narrowing the moment a scope shrinks."""
    _mock_immutable_prior_seasons(respx.mock)
    mock_identity(respx.mock)

    wide = SpyLake()
    wide.write(
        scope_envelope(
            player_ids=[CANONICAL_IDS[ALPHA], CANONICAL_IDS[BRAVO]],
            include_team_defenses=False,
        )
    )
    await capture(wide)

    narrow = SpyLake()
    narrow.write(
        scope_envelope(player_ids=[CANONICAL_IDS[ALPHA]], include_team_defenses=False)
    )
    envelopes = await capture(narrow)

    for envelope in envelopes.values():
        published = {row["player_id"] for row in envelope.signals}
        assert published == {CANONICAL_IDS[ALPHA]}, published


@respx.mock
async def test_a_scope_that_SWAPS_players_and_comes_back_does_not_re_read_twice():
    """The scenario the UNION exists for, and the only one that can see it.

    A widen-then-narrow sequence cannot: the widened keep-set is already a
    superset of the stored one, so union and replacement produce the same memo.
    The difference only appears when a scope moves SIDEWAYS and then returns —
    which is exactly what a weekly roster-scope does when a player drops out and
    comes back.

    Under replacement the two scopes ping-pong, each invalidating the other's
    memo: ~34 MB of prior-season re-reads, every alternation, forever. Under the
    union the memo grows once and covers both.
    """
    routes = _mock_immutable_prior_seasons(respx.mock)
    mock_identity(respx.mock)
    prior = [
        routes[template.format(season=season)]
        for template in (
            upstream_mod.INJURIES_URL,
            upstream_mod.SNAP_COUNTS_URL,
            upstream_mod.STATS_URL,
        )
        for season in PRIOR_SEASONS
    ]

    def lake_for(*player_ids):
        lake = SpyLake()
        lake.write(
            scope_envelope(player_ids=list(player_ids), include_team_defenses=False)
        )
        return lake

    # 1 call: unconditional first read, memo keeps {ALPHA}.
    await capture(lake_for(CANONICAL_IDS[ALPHA]))
    # 2 more: the 304, then the re-read {ALPHA} does not cover. Memo keeps
    # {ALPHA, BRAVO} under the union, {BRAVO} under replacement.
    reset_published_digests()
    await capture(lake_for(CANONICAL_IDS[BRAVO]))
    # 1 more under the union (a 304 and nothing else); 2 more under replacement,
    # because {ALPHA} would no longer be covered.
    reset_published_digests()
    await capture(lake_for(CANONICAL_IDS[ALPHA]))

    for route in prior:
        assert route.call_count == 4, (
            f"{route}: expected 4 requests (read, 304+re-read, 304) — a fifth "
            "means the keep-set was replaced rather than unioned, so the two "
            "scopes invalidate each other's memo forever"
        )


@respx.mock
async def test_the_ADAPTER_returns_only_the_keep_set_even_from_a_superset_memo():
    """The emit-side filter, pinned where it is actually observable.

    `capture.py` reads these results by looking each resolved player up by key,
    so an extra key in `by_player` is inert *today* and a capture-level assertion
    cannot see it — mutation testing confirmed that deleting the filter changed
    no published row. But the adapter's contract is "designations for
    `keep_gsis`", a caller iterating `by_player` is entirely reasonable, and the
    union means the memo genuinely holds a superset. Pinning it at the adapter is
    what stops the narrowing quietly ending at the next caller.
    """
    for season in HISTORY_SEASONS:
        respx.mock.get(upstream_mod.INJURIES_URL.format(season=season)).respond(
            200, text=injuries_csv(season)
        )

    from .conftest import BRAVO as BRAVO_GSIS
    from .conftest import CHARLIE as CHARLIE_GSIS

    async with httpx.AsyncClient() as client:
        # Build a memo covering both players...
        wide = await upstream_mod.fetch_designations(
            HISTORY_SEASONS,
            client=client,
            keep_gsis=frozenset({BRAVO_GSIS, CHARLIE_GSIS}),
        )
        assert set(wide.by_player) == {BRAVO_GSIS, CHARLIE_GSIS}

        # ...then ask for one of them. The memo covers, so nothing is re-read.
        narrow = await upstream_mod.fetch_designations(
            HISTORY_SEASONS, client=client, keep_gsis=frozenset({BRAVO_GSIS})
        )

    assert set(narrow.by_player) == {BRAVO_GSIS}, (
        "a superset memo leaked a player who is not in this pass's scope"
    )


@respx.mock
async def test_the_ADAPTER_does_not_leak_snap_rows_from_a_superset_memo():
    """The same contract, for the second of three readers.

    `Participation.snap_pct` and `.team_of` are dict-shaped and iterable exactly
    like `by_player`, and the argument that justified pinning the designation
    filter at the adapter applies verbatim. Pinning one reader and leaving two
    free is the same shape as the gap the round-1 mutations found.
    """
    for season in HISTORY_SEASONS:
        respx.mock.get(upstream_mod.SNAP_COUNTS_URL.format(season=season)).respond(
            200, text=snap_counts_csv(season)
        )

    async with httpx.AsyncClient() as client:
        wide = await upstream_mod.fetch_participation(
            HISTORY_SEASONS,
            client=client,
            keep_pfr=frozenset({BRAVO_PFR, CHARLIE_PFR}),
        )
        assert {key[0] for key in wide.snap_pct} == {BRAVO_PFR, CHARLIE_PFR}

        narrow = await upstream_mod.fetch_participation(
            HISTORY_SEASONS, client=client, keep_pfr=frozenset({BRAVO_PFR})
        )

    assert {key[0] for key in narrow.snap_pct} == {BRAVO_PFR}, (
        "a superset memo leaked snap rows for a player out of this pass's scope"
    )
    assert {key[0] for key in narrow.team_of} == {BRAVO_PFR}, (
        "team_of leaked even though snap_pct did not — both are filtered on emit"
    )


@respx.mock
async def test_the_ADAPTER_does_not_leak_production_rows_from_a_superset_memo():
    """And the third. `Production.points` completes the set."""
    for season in HISTORY_SEASONS:
        respx.mock.get(upstream_mod.STATS_URL.format(season=season)).respond(
            200, text=stats_csv(season)
        )

    from .conftest import BRAVO as BRAVO_GSIS
    from .conftest import CHARLIE as CHARLIE_GSIS

    async with httpx.AsyncClient() as client:
        wide = await upstream_mod.fetch_production(
            HISTORY_SEASONS,
            client=client,
            keep_gsis=frozenset({BRAVO_GSIS, CHARLIE_GSIS}),
        )
        assert {key[0] for key in wide.points} == {BRAVO_GSIS, CHARLIE_GSIS}

        narrow = await upstream_mod.fetch_production(
            HISTORY_SEASONS, client=client, keep_gsis=frozenset({BRAVO_GSIS})
        )

    assert {key[0] for key in narrow.points} == {BRAVO_GSIS}, (
        "a superset memo leaked production rows for a player out of scope"
    )


def test_the_scope_independent_memos_stay_scope_independent():
    """`_SCHEDULE_MEMO` and `_PLAYERS_MEMO` are keyed WITHOUT a keep-set, and
    that is only safe because neither reader narrows by scope.

    It is more structurally protected than it looks: `fetch_players` runs before
    identity resolution, so no keep-set exists at that point, and filtering
    `players.csv` by scope would need a backward `fdy -> gsis` join that reverses
    the "forward, not backward" decision in `adapters/scope.py`. Two deliberate
    changes, not one.

    Still worth eight lines, because it fails at the moment of the mistake rather
    than months later in a lake object nobody re-reads. This is the one residual
    the round-2 report flagged as untested.
    """
    import inspect

    for fetch in (upstream_mod.fetch_players, upstream_mod.fetch_schedule):
        narrowing = [
            name
            for name in inspect.signature(fetch).parameters
            if name.startswith("keep")
        ]
        assert not narrowing, (
            f"{fetch.__name__} now narrows by {narrowing}, so its season-keyed "
            "memo is only complete for the scope that built it. Carry the "
            "keep-set on the memo the way the three per-season readers do, or "
            "this is the Critical again in a file no mutation covers."
        )


def test_memo_coverage_is_a_subset_test_not_an_equality_test():
    """A superset covers; anything else does not. Equality would make every
    scope change a full re-download and a subset test the wrong way round would
    reintroduce the Critical."""
    memo = {2024: (frozenset({"a", "b"}), ())}
    assert upstream_mod._memo_covers(memo, 2024, frozenset({"a"}))
    assert upstream_mod._memo_covers(memo, 2024, frozenset({"a", "b"}))
    assert not upstream_mod._memo_covers(memo, 2024, frozenset({"a", "c"}))
    assert not upstream_mod._memo_covers(memo, 2025, frozenset({"a"}))
