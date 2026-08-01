"""The wire format: units, the active guard, the two canonicalisers, dedupe.

These are the failures that produce a *well-formed* record — money wrong by six
orders of magnitude, an expired deal published as current, a wrong club sent to
`player-identity`, or one unmapped position code failing all 500 queries in its
chunk. None of them raises anything, so none is visible from an end-to-end
assertion about counts.

The unit tests at the top are the newest and the sharpest. The CSV artifact this
collector originally read carried money as whole-dollar integers; the parquet
carries it as **doubles denominated in millions**. Both are plausible, both
parse, and getting it wrong is wrong for every row at once.
"""

import ast
import io
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import respx
from collector_core.conditional import ETAGS, UpstreamUnchanged

from player_contract.adapters import upstream

from .conftest import (
    FIXTURE_ETAG,
    PARQUET_SCHEMA,
    SEASON,
    WEEK,
    cap,
    contracts_parquet,
    mock_upstream,
    row,
)

ROOT = Path(__file__).resolve().parents[3]


async def fetch(season: int = SEASON, **kwargs):
    async with httpx.AsyncClient() as client:
        return await upstream.fetch_active_contracts(
            season, WEEK, client=client, **kwargs
        )


def serve(rows):
    return mock_upstream(respx.mock, body=contracts_parquet(rows))


def empty_parquet_missing(column: str) -> bytes:
    """A valid parquet whose schema is missing one required column."""
    narrowed = pa.schema([f for f in PARQUET_SCHEMA if f.name != column])
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pylist([], schema=narrowed), sink)
    return sink.getvalue()


# ── UNITS: the document is in millions, the contract is in dollars ──────────


def test_to_usd_scales_millions_to_whole_dollars():
    assert upstream.to_usd(150.0) == 150_000_000
    assert upstream.to_usd(0.885) == 885_000
    assert upstream.to_usd(0.0) == 0
    assert upstream.to_usd(None) is None


def test_to_usd_rounds_rather_than_truncating():
    """`int()` truncates, and a double times 1e6 lands just below the integer
    often enough to matter: measured against the live document, truncation
    corrupts **117 of 5,862** top-level money values and **553 of 29,378**
    cap-table ones.

    `4.1` is the cleanest real example — Kyle Allen's contract, which `int()`
    renders as $4,099,999. Verified that rounding is exact rather than a
    convention: every active value lands within 1e-3 of a whole dollar.
    """
    assert int(4.1 * 1_000_000) == 4_099_999  # the trap this guards
    assert upstream.to_usd(4.1) == 4_100_000
    assert upstream.to_usd(133.738375) == 133_738_375
    assert upstream.to_usd(2.521538) == 2_521_538


def test_a_money_column_that_changed_TYPE_yields_null_not_a_crash():
    """The parquet types these as doubles, so a string cannot arrive today — but
    a retyped column upstream is exactly the change that would, and a whole-pass
    `TypeError` is a worse answer than a null field with a reason. `player-stats`
    lost a season to an upstream that switched a numeric column to text."""
    assert upstream.to_usd("not a number") is None
    assert upstream.to_usd(object()) is None


def test_a_season_or_term_column_that_changed_type_yields_null_too():
    """Same argument for the counts. A fractional term is also refused rather
    than truncated — `years = 3.5` is a bad cell, not a three-year deal."""
    assert upstream._int_or_none("nonsense") is None
    assert upstream._int_or_none(object()) is None
    assert upstream._int_or_none(3.5) is None
    assert upstream._int_or_none(3.0) == 3


@respx.mock
async def test_money_is_published_in_dollars_not_the_documents_millions():
    """The whole-collector version of the two above. A fixture in whole dollars
    would let an adapter that forgot to scale pass every other test in the suite
    while publishing numbers a millionth of their true size."""
    serve([row("Rich Passer", value=150.0, guaranteed=100.0)])

    read = await fetch()

    assert read.rows[0].total_value_usd == 150_000_000
    assert read.rows[0].guaranteed_total_usd == 100_000_000


@respx.mock
async def test_the_nested_cap_table_is_scaled_too():
    """The per-season struct is in the same millions as the top-level columns.
    Scaling one and not the other is the asymmetric version of the same bug and
    is harder to spot, because the contract value would still look right."""
    serve([row("Capped", cols=[cap(SEASON, 30.25, 8.5)])])

    read = await fetch()

    assert read.rows[0].cap_hit_current_usd == 30_250_000
    assert read.rows[0].signing_bonus_proration_usd == 8_500_000


# ── is_active: upstream's flag, both arms, and it is a real bool now ────────


@respx.mock
async def test_only_active_contracts_are_kept():
    """Golf is Alpha's expired rookie deal — same player, same `otc_id`,
    `is_active` False. A collector that ignored the flag would publish a deal
    that ended in 2020 alongside the one that ends in 2029, and both rows would
    look entirely reasonable on their own."""
    mock_upstream(respx.mock)

    read = await fetch()

    assert read.rows, "the fixture produced no rows at all"
    signed = {r.player_name: r.year_signed for r in read.rows}
    assert signed["Alpha Passer"] == 2025, (
        "the historical row won, or both were kept — is_active is not a guard"
    )
    assert all(r.year_signed != 2017 for r in read.rows)


@respx.mock
async def test_an_inactive_row_is_dropped_even_when_it_is_the_only_one():
    """The other arm, in isolation: with nothing else in the document, an
    inactive row must produce an empty read rather than the whole file."""
    serve([row("Nobody Here", is_active=False)])

    assert (await fetch()).rows == []


@respx.mock
async def test_a_null_is_active_is_not_treated_as_active():
    """The parquet types `is_active` as a nullable bool. `if not raw[...]` and
    `is not True` agree on False and disagree on None — and a null flag is not a
    claim that the contract is current."""
    serve([row("Unknown Status", is_active=None)])

    assert (await fetch()).rows == []


# ── gsis_id: the Tier-1 crosswalk key the CSV did not have ──────────────────


@respx.mock
async def test_a_gsis_id_is_carried_onto_the_row():
    serve([row("Crosswalked", gsis_id="00-0012345")])
    assert (await fetch()).rows[0].gsis_id == "00-0012345"


@respx.mock
@pytest.mark.parametrize("blank", [None, "", "NA"])
async def test_a_missing_gsis_id_is_None_not_a_placeholder_string(blank):
    """`"NA"` is the feed's own null spelling and it must not reach
    `adapters/scope`, which branches on truthiness to pick a query shape — a
    literal `"NA"` would send `source_id="NA"` for every unmatched player and
    ask `player-identity` to adopt a crosswalk id that does not exist."""
    serve([row("Nameless Key", gsis_id=blank)])
    assert (await fetch()).rows[0].gsis_id is None


# ── the nested per-season cap table ─────────────────────────────────────────


@respx.mock
async def test_the_cap_entry_is_selected_by_YEAR_not_by_position_in_the_list():
    """The list is a career table running from the player's rookie season, so
    indexing it would read a different season for every player — and produce a
    perfectly well-formed cap hit from the wrong year."""
    serve(
        [
            row(
                "Long Career",
                cols=[cap(2023, 9.0, 3.0), cap(SEASON, 21.0, 5.0), cap(2027, 25.0)],
            )
        ]
    )

    read = await fetch()

    assert read.rows[0].cap_hit_current_usd == 21_000_000
    assert read.rows[0].signing_bonus_proration_usd == 5_000_000


@respx.mock
async def test_the_career_TOTAL_pseudo_row_is_never_read_as_a_season():
    """The sharpest trap in this document, and it is universal.

    Every one of the 2,250 active rows carrying a cap table ends with a
    pseudo-row whose `year` is the string `"Total"` and whose `cap_number` is
    the CAREER total — Joe Burrow's is $339,443,060 against a real 2026 cap hit
    an order of magnitude smaller. It is always the LAST element, so `cols[-1]`
    — the most natural "give me the latest" shortcut — publishes a career total
    as a current-season cap hit for every player in the league, at a magnitude
    plausible enough to survive a spot check.

    An exact match on the year is what avoids it, and this is the test that
    proves the match is exact rather than a prefix or a fallback.
    """
    serve([row("Totalled", cols=[cap(SEASON, 21.0, 5.0)])])

    read = await fetch()

    assert read.rows[0].cap_hit_current_usd == 21_000_000
    assert read.rows[0].signing_bonus_proration_usd == 5_000_000


@respx.mock
async def test_a_row_whose_ONLY_cap_entry_is_the_total_yields_null():
    """The other arm. Without it, "the total is not read" is satisfied by an
    implementation that happened to find a real season entry first — here there
    is none, so a fallback to `cols[-1]` has nothing to hide behind."""
    serve([row("Only Total", cols=[cap(2019, 5.0, 1.0)])])

    read = await fetch()

    assert read.rows[0].cap_hit_current_usd is None, (
        "the career Total was published as this season's cap hit"
    )


@respx.mock
async def test_the_year_is_matched_as_TEXT_because_the_document_stores_it_so():
    """`year` is a string inside the struct while `year_signed` beside it is an
    int32. An `==` against the int would match nothing, on every row, forever —
    and the symptom is a null cap hit, which is a legal value."""
    entry = cap(SEASON, 12.0, 4.0)
    assert isinstance(entry["year"], str)
    serve([row("Stringy Year", cols=[entry])])

    assert (await fetch()).rows[0].cap_hit_current_usd == 12_000_000


@respx.mock
async def test_a_cap_table_that_stops_before_the_season_yields_null():
    """Charlie's arm: the row has a cap table, the capture season is not in it.
    A different fact from the source carrying no cap accounting at all, and
    `capture.build_signal` reasons about the two differently."""
    serve([row("Expired Table", cols=[cap(2023, 9.0), cap(2024, 10.0)])])

    read = await fetch()

    assert read.rows[0].cap_hit_current_usd is None
    assert read.rows[0].signing_bonus_proration_usd is None


@respx.mock
async def test_a_row_with_no_cap_table_at_all_yields_null_rather_than_raising():
    """Echo's arm. 23% of live active rows carry an empty list here."""
    serve([row("No Table", cols=[])])

    assert (await fetch()).rows[0].cap_hit_current_usd is None


@respx.mock
async def test_a_present_entry_with_a_null_member_yields_null_for_that_member():
    """Delta's arm: `cap_number` is populated on 100% of 2026 entries but
    `prorated_bonus` on only 79%. One null member must not null the other."""
    serve([row("Partial", cols=[cap(SEASON, 4.5, None)])])

    read = await fetch()

    assert read.rows[0].cap_hit_current_usd == 4_500_000
    assert read.rows[0].signing_bonus_proration_usd is None


@respx.mock
async def test_a_zero_proration_is_zero_and_not_null():
    """A deal with no signing bonus prorates nothing, and that is a fact. `or
    None` on a falsy double would delete it."""
    serve([row("No Bonus", cols=[cap(SEASON, 11.125, 0.0)])])

    read = await fetch()

    assert read.rows[0].signing_bonus_proration_usd == 0
    assert read.rows[0].cap_hit_current_usd == 11_125_000


@respx.mock
async def test_the_cap_entry_follows_the_CAPTURE_season_not_a_constant():
    """A backfill of an earlier season must read that season's cap row. Pinned
    with two seasons because a hard-coded year passes any single-season test."""
    cols = [cap(2025, 12.5, 4.0), cap(2026, 30.25, 8.5)]
    serve([row("Two Seasons", cols=cols)])
    assert (await fetch(2025)).rows[0].cap_hit_current_usd == 12_500_000

    serve([row("Two Seasons", cols=cols)])
    assert (await fetch(2026)).rows[0].cap_hit_current_usd == 30_250_000


# ── dedupe ───────────────────────────────────────────────────────────────────


@respx.mock
async def test_two_active_rows_for_one_otc_id_collapse_to_the_newest():
    serve(
        [
            row("Twice Signed", otc_id=77, position="LT", year_signed=2021, value=30.0),
            row("Twice Signed", otc_id=77, position="RT", year_signed=2024, value=60.0),
        ]
    )

    read = await fetch()

    assert len(read.rows) == 1
    assert read.rows[0].year_signed == 2024
    assert read.duplicate_active == 1


@respx.mock
async def test_the_newest_row_wins_regardless_of_document_order():
    """The reversed arm. Without a total order the published row depends on the
    order the document happened to arrive in, and would flip between passes for
    no reason a reader could see."""
    serve(
        [
            row("Twice Signed", otc_id=77, position="RT", year_signed=2024, value=60.0),
            row("Twice Signed", otc_id=77, position="LT", year_signed=2021, value=30.0),
        ]
    )

    read = await fetch()

    assert len(read.rows) == 1
    assert read.rows[0].year_signed == 2024


@respx.mock
async def test_two_players_sharing_a_name_are_not_collapsed():
    """Twelve distinct names appear twice in the live active set — a QB and an
    edge rusher both called Josh Allen among them. Keying dedupe on the name
    would silently delete a player, and the survivor would look correct."""
    serve(
        [
            row("Josh Allen", otc_id=11, position="QB", team="Bills"),
            row("Josh Allen", otc_id=22, position="ED", team="Jaguars"),
        ]
    )

    read = await fetch()

    assert len(read.rows) == 2
    assert {r.otc_id for r in read.rows} == {"11", "22"}
    assert read.duplicate_active == 0


@respx.mock
async def test_the_otc_id_is_normalised_to_text():
    """int32 in the parquet, a string in the CSV this collector used to read.
    Normalised here so the published `otc_player_id` has one type regardless of
    which artifact produced it."""
    serve([row("Numeric Key", otc_id=8741)])
    assert (await fetch()).rows[0].otc_id == "8741"


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
    """The ordering is not consistent — `PHI/IND/WAS` (Wentz) is chronological
    and `IND/ATL` (Ryan) is reversed — so neither the first nor the last segment
    is reliably the current club. Publishing a coin flip would be a well-formed
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
    """The eighteen codes the live active set contains, measured against the
    parquet on 2026-08-01. Six (`ED`, `IDL`, `LT`, `RT`, `LG`, `RG`) are absent
    from `player-identity`'s vocabulary and cover 984 of the 2,931 active
    rows."""
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
async def test_apy_and_inflated_value_are_never_read():
    """Both are set to unmistakable sentinels in the fixture. `apy` is average
    annual value and a cap hit is not; `inflated_value` is restated against an
    index that moves, which would make an append-only object un-reproducible
    from its own `captured_at` — confirmed against the live parquet, where the
    two agree on the 1,793 deals signed this season and diverge on the rest."""
    mock_upstream(respx.mock)

    read = await fetch()

    assert read.rows
    for r in read.rows:
        assert r.total_value_usd != 999_999_000_000
        assert r.total_value_usd != 888_888_000_000
        assert r.cap_hit_current_usd != 999_999_000_000
    assert "apy" not in upstream.COLUMNS
    assert not any(name.startswith("inflated_") for name in upstream.COLUMNS)


@respx.mock
async def test_a_null_money_column_is_null_and_a_zero_is_zero():
    """778 active rows carry `guaranteed = 0`, which is a real fact about a deal
    with no guarantee. Mapping it to null would delete that fact; mapping a null
    to 0 would fabricate it."""
    serve(
        [
            row("No Guarantee", otc_id=1, guaranteed=0.0),
            row("Unknown Guarantee", otc_id=2, guaranteed=None),
        ]
    )

    read = await fetch()
    by_name = {r.player_name: r for r in read.rows}

    assert by_name["No Guarantee"].guaranteed_total_usd == 0
    assert by_name["Unknown Guarantee"].guaranteed_total_usd is None


@respx.mock
async def test_a_zero_length_term_is_null_rather_than_a_backwards_contract():
    """`years = 0` would make `contract_end_season` the season BEFORE the deal
    was signed — a well-formed lie. Left null, so the row publishes what it
    knows and is not counted present."""
    serve([row("Bad Term", years=0)])

    read = await fetch()

    assert len(read.rows) == 1
    assert read.rows[0].years is None


@respx.mock
async def test_a_row_with_no_player_name_is_dropped_and_counted():
    """The name is the fallback join key for the 23% of rows carrying no
    `gsis_id`. Counted rather than silently discarded — an uncounted drop shows
    up only as a scope slot with no contract, which is the same symptom as a
    player who genuinely has none."""
    serve([row("", otc_id=1), row("Real Player", otc_id=2)])

    read = await fetch()

    assert [r.player_name for r in read.rows] == ["Real Player"]
    assert read.malformed == 1


# ── the transport contract ───────────────────────────────────────────────────


@respx.mock
async def test_a_renamed_column_fails_the_pass_loudly():
    """An upstream that renames a column must not map nulls into an append-only
    lake that is never rewritten. Parquet has no header row, so this is asserted
    against the arrow schema instead."""
    mock_upstream(respx.mock, body=empty_parquet_missing("guaranteed"))

    with pytest.raises(upstream.UpstreamSchemaError):
        await fetch()


@respx.mock
async def test_a_truncated_body_raises_rather_than_yielding_half_a_league():
    """Parquet's footer is at the end, so a short body cannot be parsed at all —
    the format detects its own truncation for free, where a plain CSV silently
    yields half a document. Half of this one is a plausible league in which two
    thirds of the players have no contract."""
    whole = contracts_parquet()
    mock_upstream(respx.mock, body=whole[: len(whole) // 2])

    with pytest.raises(Exception) as raised:
        await fetch()
    assert not isinstance(raised.value, UpstreamUnchanged)


@respx.mock
async def test_a_truncated_read_does_not_commit_an_etag():
    """An ETag claims the whole document. Committing one for a short body turns
    a loud, self-retrying failure into a silent, sticky one: every later pass
    304s, `last_capture_at` advances, and the collector reports itself healthy
    while serving whatever it read once.

    `commit()` is the last statement inside the `conditional_stream` context, so
    any earlier exit simply never reaches it.
    """
    whole = contracts_parquet()
    mock_upstream(respx.mock, body=whole[: len(whole) // 2])

    with pytest.raises(Exception):
        await fetch()

    assert ETAGS.get(upstream.UPSTREAM_URL) is None


@respx.mock
async def test_a_schema_failure_does_not_commit_an_etag_either():
    """The schema check runs BEFORE `commit()` for the same reason. A collector
    that committed on the response headers would 304 forever against a document
    whose columns it cannot read."""
    mock_upstream(respx.mock, body=empty_parquet_missing("player"))

    with pytest.raises(upstream.UpstreamSchemaError):
        await fetch()

    assert ETAGS.get(upstream.UPSTREAM_URL) is None


@respx.mock
async def test_a_complete_read_commits_the_etag_and_a_304_raises_unchanged():
    """Both halves of conditional GET, verified against the live asset: it
    serves an `ETag` and answers `If-None-Match` with a `304` carrying zero
    bytes, while a ranged control returns `206`.

    Without the store nothing is saved; without `UpstreamUnchanged` a 304 would
    reach `raise_for_status()` — which gates on 2xx and DOES raise on a 304 —
    and be written as a failure.
    """
    route = mock_upstream(respx.mock)
    await fetch()
    assert ETAGS.get(upstream.UPSTREAM_URL) == FIXTURE_ETAG

    route.respond(304)
    with pytest.raises(UpstreamUnchanged):
        await fetch()


@respx.mock
async def test_the_conditional_request_actually_sends_if_none_match():
    """The half that saves the bytes. Asserted on the wire, because an ETag
    stored and never sent costs a full 6.44 MiB download on every poll while
    every other test still passes."""
    route = mock_upstream(respx.mock)
    await fetch()
    await fetch()

    assert route.call_count == 2
    assert route.calls[0].request.headers.get("if-none-match") is None
    assert route.calls[1].request.headers.get("if-none-match") == FIXTURE_ETAG


def test_the_url_and_the_etag_key_and_the_source_ref_are_one_string():
    """Three copies of a URL drift. `source_ref` is the provenance a lake object
    carries a season later, and the ETag key is what decides whether a pass
    re-downloads; they must name the same document — especially here, where the
    same release publishes two artifacts that disagree by four years."""
    assert upstream.source_ref(SEASON, WEEK) == upstream.UPSTREAM_URL
    assert upstream.source_ref(2027, 1) == upstream.UPSTREAM_URL
    assert upstream.UPSTREAM_URL.endswith(".parquet")


def test_the_read_is_projected_and_filtered_before_it_becomes_python():
    """A **structural** pin for two memory properties behaviour cannot observe.

    Both mutations below leave every published value identical — they cost only
    peak RSS — so no functional assertion in this suite can see them. Measured
    end to end against the live document, on the 256Mi (268 MB) pod limit:

        columns= dropped                 144.8 MB peak  (vs 126.6 MB)
        filter after to_pylist()         157.6 MB peak  (vs 121.7 MB)

    `roster-scope` was OOMKilled for the equivalent mistake, and neither its 171
    passing tests nor a local `docker run` could see it because neither had a
    memory limit. `tests/test_dockerfile_workspace.py` sets the precedent for
    pinning a silent non-functional mechanism structurally; this is the same
    argument. It is deliberately NOT dressed up as a behavioural test.
    """
    source = pathlib_read(upstream.__file__)
    assert "columns=sorted(COLUMNS)" in source, (
        "the parquet read stopped projecting columns; the nested `cols` column "
        "is then decoded for all 51,785 rows (+18 MB peak RSS)"
    )
    assert "batch.filter(mask).to_pylist()" in source, (
        "the batch is materialised before it is filtered; that builds a dict "
        "and a nested cap table for all 48,854 historical rows (+36 MB peak RSS)"
    )


def pathlib_read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_the_adapter_name_says_which_artifact_produced_the_row():
    """The CSV artifact of this same release is a different document with
    different units and four-year-old contents. A consumer reading two lake
    objects a season apart must be able to tell which one it has."""
    assert "parquet" in upstream.UPSTREAM_ADAPTER
