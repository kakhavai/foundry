"""Coverage — BOTH halves, because attacking only `expected` misses the bug.

`coverage.expected` must never derive from what a fetch returned. That is the
famous half and it is tested below. The other half is `present`: a set that
mutates only the floor scored 34/34 on an earlier collector and still missed a
deleted `record` predicate, so every test here that pins a denominator has a
sibling pinning the numerator.

The predicate this collector's phase doc names:

> every scoped player under an active NFL contract has a record with non-null
> `contract_end_season`

So a resolved row whose term the upstream left blank is **not** present. Delta
exists for exactly that arm.
"""

import httpx
import respx

from player_contract.capture import (
    CONTRACT_STATUS,
    EXPECTED_FLOOR,
    LEAGUE_TEAMS,
    SIGNAL_TYPES,
    capture_player_contract,
)

from .conftest import (
    CANONICAL_IDS,
    NOW,
    SCOPED_IDS,
    SEASON,
    WEEK,
    SpyLake,
    contracts_parquet,
    mock_identity,
    mock_upstream,
    row,
    scope_envelope,
)

# The one in-scope contract several tests below narrow the document to.
ALPHA_ROW = row(
    "Alpha Passer",
    otc_id=1,
    position="QB",
    team="Packers",
    year_signed=2025,
    years=5,
    gsis_id="00-0000001",
)


async def capture(lake, **kwargs):
    async with httpx.AsyncClient() as client:
        return await capture_player_contract(
            SEASON, WEEK, client=client, lake=lake, now=NOW, **kwargs
        )


def test_every_signal_type_declares_a_floor():
    assert set(EXPECTED_FLOOR) == set(SIGNAL_TYPES)
    assert all(floor >= 1 for floor in EXPECTED_FLOOR.values())


def test_the_floor_is_the_leagues_CONFIG_not_a_measurement():
    """32 teams x 12 individual scope slots. A number decided before any fetch —
    the point being that it is arithmetic over `roster-scope`'s config rather
    than anything read from a document or from the scope's own length."""
    assert EXPECTED_FLOOR[CONTRACT_STATUS] == LEAGUE_TEAMS * 12 == 384


# ── the denominator ──────────────────────────────────────────────────────────


@respx.mock
async def test_a_truncated_upstream_does_not_report_full_coverage(lake):
    """The failure the floor exists for. One contract row out of a league's
    worth must not yield `expected: 1, present: 1`, ratio 1.0 — which is what a
    collector expecting one key per *fetched row* would report while 99% of the
    league silently vanished."""
    rows = [ALPHA_ROW]
    mock_upstream(respx.mock, body=contracts_parquet(rows))
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]

    assert envelope.coverage.expected == EXPECTED_FLOOR[CONTRACT_STATUS]
    assert envelope.coverage.present == 1
    assert envelope.coverage.ratio < 1.0
    reasons = {error["reason"] for error in envelope.errors}
    assert "below_expected_floor" in reasons, reasons


@respx.mock
async def test_a_truncated_SCOPE_does_not_report_full_coverage():
    """The same failure arriving from the other side. `Coverage.ratio` is 1.0
    when `expected` is 0, so a scope naming two players would read as a perfect
    pass if the expectation came from the scope's own length."""
    lake = SpyLake()
    lake.write(
        scope_envelope(
            player_ids=[CANONICAL_IDS["Alpha Passer"]], include_team_defenses=False
        )
    )
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]

    assert envelope.coverage.expected == EXPECTED_FLOOR[CONTRACT_STATUS]
    assert envelope.coverage.present == 1
    assert envelope.coverage.ratio < 1.0


@respx.mock
async def test_an_empty_upstream_reports_zero_not_one(lake):
    """`Coverage.ratio` returns 1.0 when `expected` is 0 — correct for a bye
    week, catastrophic for a pass that captured nothing."""
    mock_upstream(respx.mock, body=contracts_parquet([]))
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]

    assert envelope.coverage.present == 0
    assert envelope.coverage.expected == EXPECTED_FLOOR[CONTRACT_STATUS]
    assert envelope.coverage.ratio == 0.0


@respx.mock
async def test_expansion_past_the_floor_still_reports_honestly():
    """The floor must not CAP a genuine count, only raise a short one. A scope
    larger than the config (a roster-scope rule change) has to report the real
    denominator."""
    extra = [f"fdy-extra{index:08d}" for index in range(500)]
    lake = SpyLake()
    lake.write(scope_envelope(player_ids=extra, include_team_defenses=False))
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]

    assert envelope.coverage.expected == 500
    assert envelope.coverage.expected > EXPECTED_FLOOR[CONTRACT_STATUS]


# ── the numerator ────────────────────────────────────────────────────────────


@respx.mock
async def test_only_a_record_with_a_KNOWN_END_SEASON_counts_as_present(lake):
    """The phase doc's predicate, and the half a floor-only mutation set misses.

    Delta's `years` is blank, so his record publishes — team, start season and
    money are all real — but it does not answer "when does this deal end", which
    is the question. Six players are in scope; five have a known term.
    """
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]

    assert len(envelope.signals) == 6, "Delta's row was dropped rather than published"
    assert envelope.coverage.present == 5
    assert f"player:{CANONICAL_IDS['Delta Blocker']}" in envelope.coverage.missing
    reasons = {error["reason"] for error in envelope.errors}
    assert "contract_term_unknown" in reasons, reasons


@respx.mock
async def test_a_scope_slot_with_no_contract_row_is_named_and_reasoned(lake):
    """The narrowing hole, made visible. An unresolved, out-of-feed or
    practice-squad player is a HOLE against the watchlist rather than a row that
    quietly disappeared from both numerator and denominator.

    See `capture.py`'s module docstring, section 3, for why this is reported
    rather than excluded from `expected` as the phase doc's wording asks.
    """
    rows = [ALPHA_ROW]
    mock_upstream(respx.mock, body=contracts_parquet(rows))
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]

    missing = set(envelope.coverage.missing)
    assert missing == {
        f"player:{pid}" for pid in SCOPED_IDS if pid != CANONICAL_IDS["Alpha Passer"]
    }
    reasons = {error["reason"] for error in envelope.errors}
    assert "no_active_contract" in reasons, reasons


@respx.mock
async def test_an_out_of_scope_row_never_enters_the_numerator(lake):
    """Foxtrot resolves. If he were recorded, `present` would count a player the
    watchlist never asked for — inflating the ratio with somebody else's
    contract."""
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]

    assert envelope.coverage.present <= len(SCOPED_IDS)
    assert f"player:{CANONICAL_IDS['Foxtrot Wideout']}" not in envelope.coverage.missing


@respx.mock
async def test_present_can_never_exceed_the_scope(lake):
    """A pass that resolved the whole feed still owes only what the watchlist
    named. Cheap, and it is what fails if `record` is ever called before the
    scope check."""
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]

    assert envelope.coverage.present <= envelope.coverage.expected
