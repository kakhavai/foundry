"""The wire format: the active-contract guard, the two canonicalisers, dedupe.

These are the failures that produce a *well-formed* record — an expired deal
published as current, a wrong club sent to `player-identity`, or one unmapped
position code failing all 500 queries in its chunk. None of them raises
anything, so none is visible from an end-to-end assertion about counts.
"""

import ast
import gzip
from pathlib import Path

import httpx
import pytest
import respx
from collector_core.conditional import ETAGS, UpstreamUnchanged
from collector_core.streaming import UpstreamSchemaError, UpstreamTruncated

from player_contract.adapters import upstream

from .conftest import (
    CSV_COLUMNS,
    SEASON,
    WEEK,
    contracts_csv,
    contracts_gz,
    mock_upstream,
)

ROOT = Path(__file__).resolve().parents[3]


async def fetch(**kwargs):
    async with httpx.AsyncClient() as client:
        return await upstream.fetch_active_contracts(
            SEASON, WEEK, client=client, **kwargs
        )


# ── is_active: upstream's flag, and both arms have a fixture ─────────────────


@respx.mock
async def test_only_active_contracts_are_kept():
    """Golf is Alpha's expired rookie deal — same player, same `otc_id`,
    `is_active` FALSE. A collector that ignored the flag would publish a deal
    that ended in 2020 alongside the one that ends in 2029, and both rows would
    look entirely reasonable on their own."""
    mock_upstream(respx.mock)
    read = await fetch()

    assert read.rows, "the fixture produced no rows at all"
    signed = {row.player_name: row.year_signed for row in read.rows}
    assert signed["Alpha Passer"] == 2025, (
        "the historical row won, or both were kept — is_active is not a guard"
    )
    assert all(row.year_signed != 2017 for row in read.rows)


@respx.mock
async def test_a_row_flagged_inactive_is_dropped_even_when_it_is_the_only_one():
    """The other arm, in isolation: with nothing else in the document, an
    inactive row must produce an empty read rather than the whole file."""
    body = gzip.compress(
        contracts_csv(
            [("9", "Nobody Here", "QB", "Bears", "FALSE", 2019, 3, 1, 1)]
        ).encode()
    )
    mock_upstream(respx.mock, body=body)

    read = await fetch()

    assert read.rows == []


@respx.mock
@pytest.mark.parametrize("flag", ["TRUE", "true", "True", "1"])
async def test_active_is_matched_case_insensitively(flag):
    body = gzip.compress(
        contracts_csv(
            [("9", "Somebody", "QB", "Bears", flag, 2025, 3, 1000, 500)]
        ).encode()
    )
    mock_upstream(respx.mock, body=body)

    read = await fetch()

    assert [row.player_name for row in read.rows] == ["Somebody"]


@respx.mock
async def test_the_string_FALSE_is_not_truthy():
    """`bool("FALSE")` is `True`. That mistake publishes all 31,893 historical
    contracts as current, and every one of them is a well-formed record."""
    body = gzip.compress(
        contracts_csv(
            [("9", "Somebody", "QB", "Bears", "FALSE", 2025, 3, 1000, 500)]
        ).encode()
    )
    mock_upstream(respx.mock, body=body)

    assert (await fetch()).rows == []


# ── dedupe ───────────────────────────────────────────────────────────────────


@respx.mock
async def test_two_active_rows_for_one_otc_id_collapse_to_the_newest():
    """21 `otc_id`s carry two active rows in the real document. Both arms are
    asserted below so an implementation that simply takes the first is not
    accidentally correct here."""
    rows = [
        ("77", "Twice Signed", "LT", "Rams", "TRUE", 2021, 3, 30000000, 10000000),
        ("77", "Twice Signed", "RT", "Rams", "TRUE", 2024, 4, 60000000, 20000000),
    ]
    mock_upstream(respx.mock, body=gzip.compress(contracts_csv(rows).encode()))

    read = await fetch()

    assert len(read.rows) == 1
    assert read.rows[0].year_signed == 2024
    assert read.duplicate_active == 1


@respx.mock
async def test_the_newest_row_wins_regardless_of_document_order():
    """The reversed arm. Without a total order the published row depends on the
    order the document happened to arrive in, and would flip between passes for
    no reason a reader could see."""
    rows = [
        ("77", "Twice Signed", "RT", "Rams", "TRUE", 2024, 4, 60000000, 20000000),
        ("77", "Twice Signed", "LT", "Rams", "TRUE", 2021, 3, 30000000, 10000000),
    ]
    mock_upstream(respx.mock, body=gzip.compress(contracts_csv(rows).encode()))

    read = await fetch()

    assert len(read.rows) == 1
    assert read.rows[0].year_signed == 2024


@respx.mock
async def test_two_players_sharing_a_name_are_not_collapsed():
    """38 names appear twice in the real active set — a QB and an edge rusher
    both called Josh Allen among them. Keying dedupe on the name would silently
    delete a player, and the survivor would look perfectly correct."""
    rows = [
        ("11", "Josh Allen", "QB", "Bills", "TRUE", 2025, 6, 250000000, 150000000),
        ("22", "Josh Allen", "ED", "Jaguars", "TRUE", 2024, 5, 150000000, 80000000),
    ]
    mock_upstream(respx.mock, body=gzip.compress(contracts_csv(rows).encode()))

    read = await fetch()

    assert len(read.rows) == 2
    assert {row.otc_id for row in read.rows} == {"11", "22"}
    assert read.duplicate_active == 0


# ── canonical_team ───────────────────────────────────────────────────────────


def test_the_nickname_table_covers_all_32_clubs_and_only_canonical_codes():
    """All 32 clubs appear in the active set as nicknames, and the rest of the
    fleet keys on abbreviations — so a mapping that lost one would publish nulls
    for an entire team and score every one of its players as *disagreeing* on
    `team` at `player-identity`.

    Read out of `roster-scope`'s own module rather than duplicated, so a
    canonical code changing there fails here instead of drifting.
    """
    source = (
        ROOT / "services" / "roster-scope" / "roster_scope" / "rules.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    teams = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and getattr(node.target, "id", "") == "TEAMS"
        ):
            teams = set(ast.literal_eval(node.value))
    assert teams and len(teams) == 32, "TEAMS not found in roster_scope.rules"

    mapped = set(upstream.OTC_TEAM_NICKNAMES.values())
    assert mapped == teams, mapped.symmetric_difference(teams)
    assert len(upstream.OTC_TEAM_NICKNAMES) == 32


@pytest.mark.parametrize(
    ("label", "expected"),
    [("Packers", "GB"), ("49ers", "SF"), ("Commanders", "WAS")],
)
def test_a_single_club_nickname_resolves(label, expected):
    assert upstream.canonical_team(label) == expected


@pytest.mark.parametrize("label", ["DEN/SEA", "IND/ATL", "PHI/IND/WAS"])
def test_a_multi_club_string_is_null_rather_than_a_guess(label):
    """The ordering is not consistent — `PHI/IND/WAS` is chronological and
    `IND/ATL` is reversed — so neither the first nor the last segment is
    reliably the current club. Publishing a coin flip would be a well-formed
    record naming a team the player does not play for."""
    assert upstream.canonical_team(label) is None


def test_an_unrecognised_club_label_is_null_rather_than_passed_through():
    """A wrong team is worse than an absent one: `team` carries 0.20 of
    `player-identity`'s resolution weight and scores as DISAGREEMENT, so
    `PACKERS` would actively push a correct match below threshold."""
    assert upstream.canonical_team("Bratislava Fog") is None
    assert upstream.canonical_team("") is None


# ── canonical_position: the 422-the-whole-batch guard ────────────────────────


def _known_positions() -> set[str]:
    """`player-identity`'s own `KNOWN_POSITIONS`, read by AST.

    Not imported: `player-identity` is a separate uv package and is not a
    dependency of this one. Parsed rather than copied, because a copy is exactly
    what would let the two drift into a mapping that 422s in production and
    passes here.
    """
    source = (
        ROOT / "services" / "player-identity" / "player_identity" / "identity.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.AnnAssign):
            target = getattr(node.target, "id", "")
        elif isinstance(node, ast.Assign) and node.targets:
            target = getattr(node.targets[0], "id", "")
        if target == "_POSITION_GROUPS":
            return set(ast.literal_eval(node.value))
    raise AssertionError("_POSITION_GROUPS not found in player_identity.identity")


def test_every_mapped_position_is_one_player_identity_actually_knows():
    """The load-bearing drift gate. `build_query` raises 422 on an unknown
    position and `/resolve/batch` validates the whole body, so ONE bad code
    fails all 500 queries in its chunk — recorded as an `identity_upstream_error`
    against every one of them, which reads like an outage."""
    known = _known_positions()
    assert known, "the AST read found nothing; this test would pass vacuously"
    unknown = set(upstream.OTC_POSITIONS.values()) - known
    assert not unknown, unknown


def test_the_real_documents_position_codes_are_all_mapped():
    """The eighteen codes the live active set actually contains, measured
    2026-08-01. Six of them (`ED`, `IDL`, `LT`, `RT`, `LG`, `RG`) are absent from
    `player-identity`'s vocabulary and cover 952 of the 2,908 active rows."""
    observed = {
        "WR", "CB", "IDL", "LB", "ED", "S", "TE", "RB", "LT", "QB",
        "RT", "LG", "RG", "C", "K", "P", "LS", "FB",
    }  # fmt: skip
    assert observed <= set(upstream.OTC_POSITIONS)


def test_an_unmapped_code_becomes_null_rather_than_being_passed_through():
    """`None` costs one row 0.15 of its resolution score. Passing the raw code
    through costs 500 rows their entire resolution."""
    assert upstream.canonical_position("XLB") is None
    assert upstream.canonical_position("") is None


# ── parsing ──────────────────────────────────────────────────────────────────


@respx.mock
async def test_money_is_taken_as_whole_dollars_and_not_rescaled():
    mock_upstream(respx.mock)
    read = await fetch()
    alpha = next(r for r in read.rows if r.player_name == "Alpha Passer")
    assert alpha.total_value_usd == 150000000
    assert alpha.guaranteed_total_usd == 100000000


@respx.mock
async def test_apy_and_inflated_value_are_never_read():
    """Both are set to unmistakable sentinels in the fixture. `apy` is average
    annual value and a cap hit is not; `inflated_value` is restated against an
    index that moves, which would make an append-only object un-reproducible."""
    mock_upstream(respx.mock)
    read = await fetch()
    for row in read.rows:
        assert row.total_value_usd != 999_999_999
        assert row.total_value_usd != 888_888_888
        assert row.guaranteed_total_usd != 999_999_999
    assert "apy" not in upstream.COLUMNS
    assert not any(name.startswith("inflated_") for name in upstream.COLUMNS)


@respx.mock
async def test_a_blank_money_column_is_null_and_a_zero_is_zero():
    """894 active rows carry `guaranteed = 0`, which is a real fact about a deal
    with no guarantee. Mapping it to null would delete that fact; mapping a
    blank to 0 would fabricate it."""
    rows = [
        ("1", "No Guarantee", "QB", "Bears", "TRUE", 2025, 2, 5000000, 0),
        ("2", "Unknown Guarantee", "QB", "Bears", "TRUE", 2025, 2, 5000000, None),
    ]
    mock_upstream(respx.mock, body=gzip.compress(contracts_csv(rows).encode()))

    read = await fetch()
    by_name = {r.player_name: r for r in read.rows}

    assert by_name["No Guarantee"].guaranteed_total_usd == 0
    assert by_name["Unknown Guarantee"].guaranteed_total_usd is None


@respx.mock
async def test_a_spreadsheet_round_tripped_integer_still_parses():
    """`5.0` for a five-year deal, `12000000.0` for a value. One upstream tool
    change would otherwise null every money column and every term at once — and
    a whole-league null reads as a shape change rather than as a bug."""
    rows = [("1", "Rounded", "QB", "Bears", "TRUE", "2025.0", "3.0", "5000000.0", "0")]
    mock_upstream(respx.mock, body=gzip.compress(contracts_csv(rows).encode()))

    read = await fetch()

    assert read.rows[0].year_signed == 2025
    assert read.rows[0].years == 3
    assert read.rows[0].total_value_usd == 5000000


@respx.mock
async def test_a_fractional_or_unparseable_value_is_null_rather_than_truncated():
    """`int(4.7)` is 4. A dollar figure that arrived fractional means the column
    changed shape, and silently truncating it publishes a number nobody
    produced."""
    rows = [("1", "Odd", "QB", "Bears", "TRUE", 2025, 3, "4.7", "banana")]
    mock_upstream(respx.mock, body=gzip.compress(contracts_csv(rows).encode()))

    read = await fetch()

    assert read.rows[0].total_value_usd is None
    assert read.rows[0].guaranteed_total_usd is None


@respx.mock
async def test_a_zero_length_term_is_null_rather_than_a_backwards_contract():
    """`years = 0` would make `contract_end_season` the season BEFORE the deal
    was signed — a well-formed lie. Left null, so the row publishes what it
    knows and is not counted present."""
    rows = [("1", "Bad Term", "QB", "Bears", "TRUE", 2025, 0, 5000000, 0)]
    mock_upstream(respx.mock, body=gzip.compress(contracts_csv(rows).encode()))

    read = await fetch()

    assert len(read.rows) == 1
    assert read.rows[0].years is None


@respx.mock
async def test_a_row_with_no_player_name_is_dropped_and_counted():
    """The name is the join key; without it `player-identity` has nothing to
    resolve. Counted rather than silently discarded — an uncounted drop shows up
    only as a scope slot with no contract, which is the same symptom as a player
    who genuinely has none."""
    rows = [
        ("1", "", "QB", "Bears", "TRUE", 2025, 2, 5000000, 0),
        ("2", "Real Player", "QB", "Bears", "TRUE", 2025, 2, 5000000, 0),
    ]
    mock_upstream(respx.mock, body=gzip.compress(contracts_csv(rows).encode()))

    read = await fetch()

    assert [r.player_name for r in read.rows] == ["Real Player"]
    assert read.malformed == 1


# ── the streaming contract ───────────────────────────────────────────────────


@respx.mock
async def test_a_renamed_column_fails_the_pass_loudly():
    """An upstream that renames a column must not map nulls into an append-only
    lake that is never rewritten."""
    header = ",".join(c for c in CSV_COLUMNS if c != "guaranteed")
    body = gzip.compress(f"{header}\n".encode())
    mock_upstream(respx.mock, body=body)

    with pytest.raises(UpstreamSchemaError):
        await fetch()


@respx.mock
async def test_a_truncated_gzip_body_raises_rather_than_yielding_half_a_league():
    """The correctness property `gzipped=True` buys. Half of this document is a
    plausible league in which two thirds of the players have no contract, and a
    plain-CSV read could not detect it at all."""
    whole = contracts_gz()
    mock_upstream(respx.mock, body=whole[: len(whole) // 2])

    with pytest.raises(UpstreamTruncated):
        await fetch()


@respx.mock
async def test_a_truncated_read_does_not_commit_an_etag():
    """An ETag claims the whole document. Committing one for a short body turns
    a loud, self-retrying failure into a silent, sticky one: every later pass
    304s, `last_capture_at` advances, and the collector reports itself healthy
    while serving whatever it read once."""
    whole = contracts_gz()
    mock_upstream(respx.mock, body=whole[: len(whole) // 2])

    with pytest.raises(UpstreamTruncated):
        await fetch()

    assert ETAGS.get(upstream.UPSTREAM_URL) is None


@respx.mock
async def test_a_complete_read_commits_the_etag_and_a_304_raises_unchanged():
    """Both halves of conditional GET. Without the store nothing is saved;
    without `UpstreamUnchanged` a 304 would reach `raise_for_status()` — which
    gates on 2xx and DOES raise on a 304 — and be written as a failure."""
    route = mock_upstream(respx.mock)
    await fetch()
    assert ETAGS.get(upstream.UPSTREAM_URL) == '"fixture-etag"'

    route.respond(304)
    with pytest.raises(UpstreamUnchanged):
        await fetch()


@respx.mock
async def test_the_row_dicts_carry_only_the_projected_columns():
    """`columns=` is what keeps a 24-column document from building a 24-key dict
    for each of 31,893 rows. Proven through the parsed row rather than by
    reading the argument, so removing the argument fails here."""
    mock_upstream(respx.mock)
    read = await fetch()
    assert read.rows
    # `season_history` is in the document, is empty on all 31,893 rows, and is
    # the field the deferred `player-incentives` collector would have needed.
    # Nothing in this collector's row shape carries it.
    assert not hasattr(read.rows[0], "season_history")


def test_the_url_and_the_etag_key_and_the_source_ref_are_one_string():
    """Three copies of a URL drift. `source_ref` is the provenance a lake object
    carries a season later, and the ETag key is what decides whether a pass
    re-downloads; they must name the same document."""
    assert upstream.source_ref(SEASON, WEEK) == upstream.UPSTREAM_URL
    assert upstream.source_ref(2027, 1) == upstream.UPSTREAM_URL
