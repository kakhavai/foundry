"""Producer-side contract conformance, against the **real** capture path.

The repo-root `tests/test_signal_envelope_conformance.py` validates committed
static fixtures — both sides hand-maintained — so it catches fixture drift and
never producer drift. A field renamed in `capture.py` leaves it entirely green.
This file closes that gap by running the real capture path and validating the
rows it actually emits, on the degraded paths as well as the happy one.

The schema is doing real work here rather than restating the code. Two clauses
in particular are enforcement, not documentation:

* `player_id` is pinned to `^fdy-`. The upstream keys players by display NAME,
  so the single most dangerous bug available to this collector is publishing a
  raw upstream string as an identity. That pattern fails the conformance test
  rather than the generator six weeks later.
* the six cap-accounting fields are typed `"null"`, not `["integer", "null"]`.
  Fabricating one — from `apy`, most plausibly — becomes a contract violation.
"""

import json
from pathlib import Path

import httpx
import jsonschema
import pytest
import respx
from jsonschema import Draft202012Validator, FormatChecker

from player_contract.capture import (
    CONTRACT_STATUS,
    EXPECTED_FLOOR,
    SIGNAL_TYPES,
    UNDERIVED_FIELDS,
    UNSOURCED_FIELDS,
    capture_player_contract,
)

from .conftest import (
    CANONICAL_IDS,
    NOW,
    SEASON,
    WEEK,
    contracts_parquet,
    mock_identity,
    mock_upstream,
    row,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "signal-envelope"
ENVELOPE_SCHEMA = json.loads((CONTRACTS / "envelope.v1.schema.json").read_text())
FIELD_SCHEMAS = json.loads(
    (CONTRACTS / "collectors" / "player-contract.json").read_text(),
)["signal_types"]


def validate(envelopes: dict) -> None:
    for signal_type, envelope in envelopes.items():
        body = envelope.to_dict()
        jsonschema.validate(body, ENVELOPE_SCHEMA)
        validator = Draft202012Validator(
            FIELD_SCHEMAS[signal_type], format_checker=FormatChecker()
        )
        for signal in body["signals"]:
            validator.validate(signal)


async def capture(lake, **kwargs):
    async with httpx.AsyncClient() as client:
        return await capture_player_contract(
            SEASON, WEEK, client=client, lake=lake, now=NOW, **kwargs
        )


@respx.mock
async def test_a_complete_capture_conforms(lake):
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    assert set(envelopes) == set(SIGNAL_TYPES)
    for envelope in envelopes.values():
        assert envelope.signals, "nothing captured to validate"
    validate(envelopes)


@respx.mock
async def test_every_envelope_is_written_to_the_lake(lake):
    """An envelope that is served but never written leaves no record a week
    later, and the lake is the only durable copy."""
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    await capture(lake)

    written = {e.signal_type for e in lake.writes if e.collector == "player-contract"}
    assert written == set(SIGNAL_TYPES)


@respx.mock
async def test_a_failed_capture_writes_a_present_zero_envelope(lake):
    """The contract: a poll that fails writes an envelope with
    `coverage.present: 0` and a populated `errors` array, so a gap in the lake
    is explicit rather than inferred from absence — then re-raises, so the last
    good capture is not overwritten by an empty one."""
    mock_upstream(respx.mock, status=503)
    mock_identity(respx.mock)

    with pytest.raises(httpx.HTTPStatusError):
        await capture(lake)

    written = [e for e in lake.writes if e.collector == "player-contract"]
    assert {e.signal_type for e in written} == set(SIGNAL_TYPES)
    for envelope in written:
        jsonschema.validate(envelope.to_dict(), ENVELOPE_SCHEMA)
        assert envelope.coverage.present == 0
        assert envelope.coverage.expected >= 1, (
            "expected: 0 makes Coverage.ratio read 1.0 — a total outage would "
            "report perfect coverage"
        )
        assert envelope.coverage.expected == EXPECTED_FLOOR[envelope.signal_type]
        assert envelope.errors, "a failure envelope with no errors explains nothing"


@respx.mock
async def test_a_degraded_capture_still_conforms(lake):
    """A pass where the identity seam is down and half the terms are blank still
    has to produce rows the generator can read — degraded paths are where a
    schema violation actually escapes, because the happy path is the one
    everybody looks at."""
    rows = [
        row("Alpha Passer", otc_id=1, team="Packers", year_signed=None,
            years=None, value=None, guaranteed=None,
            gsis_id="00-0000001"),
        row("Bravo Runner", otc_id=2, position="RB", team="Bratislava Fog",
            year_signed=2023, years=4, value=0.0, guaranteed=0.0,
            gsis_id="00-0000002"),
    ]  # fmt: skip
    mock_upstream(respx.mock, body=contracts_parquet(rows))
    mock_identity(respx.mock)

    envelopes = await capture(lake)

    assert envelopes[CONTRACT_STATUS].signals
    validate(envelopes)


def test_the_schema_covers_exactly_the_declared_signal_types():
    """A schema that silently omits a signal type would let that type's rows go
    unvalidated by both this file and the repo-root suite."""
    assert set(FIELD_SCHEMAS) == set(SIGNAL_TYPES)


def test_the_deferred_incentive_half_has_no_surface_here():
    """`player_incentive_progress` was split out during 8E — the free feed
    carries no incentive data of any kind. Pinned so a later edit cannot
    reintroduce an empty signal type, an empty route or a stub field set that
    would make the deferral invisible."""
    assert SIGNAL_TYPES == (CONTRACT_STATUS,)
    document = json.dumps(FIELD_SCHEMAS).lower()
    for token in ("incentive", "escalator", "ltbe", "nltbe", "threshold"):
        assert token not in document, token


# ── the THREE kinds of null, one fixture per arm ─────────────────────────────


@respx.mock
async def test_the_permanently_null_fields_are_present_and_null_on_every_row(lake):
    """Present and null, never omitted. An absent key cannot distinguish "this
    source will never supply it" from "this row did not say", and a consumer
    deciding whether to buy a cap-accounting feed needs exactly that
    distinction.

    Four fields, not the phase doc's six: the parquet's per-season cap table
    supplies `cap_hit_current_usd` and `signing_bonus_proration_usd`, which are
    asserted separately below.
    """
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]

    assert envelope.signals, "no rows; the loop below would pass vacuously"
    assert len(UNSOURCED_FIELDS) == 3
    assert len(UNDERIVED_FIELDS) == 1
    for signal in envelope.signals:
        for name in UNSOURCED_FIELDS:
            assert name in signal, name
            assert signal[name] is None, name
            assert signal["null_field_reasons"][name] == "unsourced_by_upstream"
        for name in UNDERIVED_FIELDS:
            assert name in signal, name
            assert signal[name] is None, name
            assert signal["null_field_reasons"][name] == "requires_undefined_derivation"


@respx.mock
async def test_a_sourced_cap_hit_is_published_and_carries_NO_null_reason(lake):
    """The field the phase doc calls unsourceable, sourced.

    Alpha's 2026 entry is 30.25 in the document's millions. A `null_field_reasons`
    entry for a populated field would make the whole block meaningless — a
    consumer filtering on it would discard a real number.
    """
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]
    alpha = next(
        s for s in envelope.signals if s["player_id"] == CANONICAL_IDS["Alpha Passer"]
    )

    assert alpha["cap_hit_current_usd"] == 30_250_000
    assert alpha["signing_bonus_proration_usd"] == 8_500_000
    assert "cap_hit_current_usd" not in alpha["null_field_reasons"]
    assert "signing_bonus_proration_usd" not in alpha["null_field_reasons"]


@respx.mock
async def test_a_cap_field_absent_for_THIS_season_is_reasoned_as_a_ROW_gap(lake):
    """The distinction that made the third reason value necessary.

    Charlie's cap table stops in 2024, so his 2026 cap hit is null — but not
    because the source lacks cap accounting. Emitting `unsourced_by_upstream`
    here would be a machine-readable falsehood: it tells a consumer that buying
    a feed would not help, when in fact this very document supplies the field
    for three quarters of the league.
    """
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]
    charlie = next(
        s
        for s in envelope.signals
        if s["player_id"] == CANONICAL_IDS["Charlie Catcher"]
    )

    assert charlie["cap_hit_current_usd"] is None
    reasons = charlie["null_field_reasons"]
    assert reasons["cap_hit_current_usd"] == "absent_in_upstream_row"
    assert reasons["dead_money_if_cut_usd"] == "unsourced_by_upstream"
    assert reasons["guaranteed_remaining_usd"] == "requires_undefined_derivation"


@respx.mock
async def test_a_ZERO_or_FALSE_field_is_present_data_and_gets_no_null_reason(lake):
    """The reason predicate must test `is None`, never truthiness.

    `if not signal[name]` reads identically and is wrong for the most common
    rows in the league. Against the live document it would stamp
    `absent_in_upstream_row` on:

    * `seasons_remaining` for the **1,273 players in a contract year**, whose
      value is exactly `0`;
    * `is_contract_year` for everyone it is `False` for — most of the league;
    * `guaranteed_total_usd` for the **778 rows with no guarantee**, where `0`
      is the fact;
    * every zero `signing_bonus_proration_usd`.

    That is precisely the machine-readable falsehood the whole sourced-cap-field
    expansion was justified by preventing: it tells a consumer "this row did not
    say" about a number the row states outright. Every other test here inspects
    a field that is either null or truthy, so none of them can see it — the
    mutant survived all 166 before this existed.

    Bravo is the fixture: his deal ends in the capture season, so
    `seasons_remaining` is `0` and `is_contract_year` is `True`, and his 2026
    cap entry carries a proration of exactly `0.0`.
    """
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]
    bravo = next(
        s for s in envelope.signals if s["player_id"] == CANONICAL_IDS["Bravo Runner"]
    )
    alpha = next(
        s for s in envelope.signals if s["player_id"] == CANONICAL_IDS["Alpha Passer"]
    )

    # Preconditions, so the assertions below cannot pass vacuously against a
    # fixture that quietly stopped producing falsy values.
    assert bravo["seasons_remaining"] == 0
    assert bravo["signing_bonus_proration_usd"] == 0
    assert bravo["is_contract_year"] is True
    assert alpha["is_contract_year"] is False

    reasons = bravo["null_field_reasons"]
    assert "seasons_remaining" not in reasons, (
        "a contract year (seasons_remaining == 0) was reported as data the "
        "upstream did not supply"
    )
    assert "signing_bonus_proration_usd" not in reasons
    assert "is_contract_year" not in alpha["null_field_reasons"]


@respx.mock
async def test_a_zero_guarantee_is_data_not_an_absent_field(lake):
    """The same predicate, on the field where zero is most obviously a fact:
    778 active rows carry `guaranteed = 0`, meaning a deal with no guarantee.
    Its own test because it needs its own fixture — every scoped player in the
    default population has a non-zero guarantee."""
    mock_upstream(
        respx.mock,
        body=contracts_parquet(
            [
                row(
                    "Alpha Passer",
                    otc_id=1,
                    team="Packers",
                    year_signed=2025,
                    years=5,
                    guaranteed=0.0,
                    gsis_id="00-0000001",
                )
            ]
        ),
    )
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]
    alpha = next(
        s for s in envelope.signals if s["player_id"] == CANONICAL_IDS["Alpha Passer"]
    )

    assert alpha["guaranteed_total_usd"] == 0
    assert "guaranteed_total_usd" not in alpha["null_field_reasons"], (
        "a deal with no guarantee was reported as one whose guarantee the "
        "upstream did not state"
    )


@respx.mock
async def test_all_three_reasons_can_appear_on_ONE_row(lake):
    """Echo carries every arm at once: a blank `guaranteed` and an ambiguous
    multi-club `team` (row gaps), no cap table at all (also a row gap),
    `tag_status` (never sourced) and `guaranteed_remaining_usd` (underived).

    A single row proving all three is what stops the three values collapsing
    back into one over time — the collapse is invisible per-field.
    """
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]
    echo = next(
        s for s in envelope.signals if s["player_id"] == CANONICAL_IDS["Echo Kicker"]
    )

    reasons = echo["null_field_reasons"]
    assert echo["guaranteed_total_usd"] is None
    assert reasons["guaranteed_total_usd"] == "absent_in_upstream_row"
    assert reasons["team"] == "absent_in_upstream_row"
    assert reasons["cap_hit_current_usd"] == "absent_in_upstream_row"
    assert reasons["tag_status"] == "unsourced_by_upstream"
    assert reasons["guaranteed_remaining_usd"] == "requires_undefined_derivation"
    assert len(set(reasons.values())) == 3, reasons


@respx.mock
async def test_a_populated_field_gets_no_null_reason(lake):
    """A reason for a field that is not null would make the block meaningless —
    a consumer filtering on it would discard perfectly good numbers."""
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]
    alpha = next(
        row
        for row in envelope.signals
        if row["player_id"] == CANONICAL_IDS["Alpha Passer"]
    )

    assert alpha["total_value_usd"] == 150000000
    assert "total_value_usd" not in alpha["null_field_reasons"]
    assert "team" not in alpha["null_field_reasons"]
    for name in UNSOURCED_FIELDS:
        assert name in alpha["null_field_reasons"]


@respx.mock
async def test_no_cap_field_is_ever_derived_from_apy(lake):
    """`apy` is average annual value; a cap hit is not, and the two agreeing on
    some rows is exactly what makes the substitution dangerous. The fixture sets
    `apy` to an unmistakable sentinel and the column is not even read."""
    mock_upstream(respx.mock)
    mock_identity(respx.mock)

    envelope = (await capture(lake))[CONTRACT_STATUS]

    assert "999999999" not in json.dumps(envelope.to_dict())
