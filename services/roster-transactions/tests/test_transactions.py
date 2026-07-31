"""The transaction vocabulary, the derived id, and row validation.

No HTTP and no lake: these are rules, and rules deserve tests that fail for one
reason each.
"""

from datetime import UTC, datetime, timedelta

import pytest

from roster_transactions.transactions import (
    CONFIDENCE_LEVELS,
    TRANSACTION_TYPES,
    TransactionSchemaError,
    UnknownTransactionType,
    duplicate_signing_count,
    normalize,
    parse_timestamp,
    transaction_id,
)

BASE = {
    "transaction_type": "signing",
    "player_id": "fdy-0001",
    "position": "wr",
    "from_team": "",
    "to_team": "kc",
    "announced_at": "2026-09-01T12:00:00Z",
    "effective_at": "2026-09-02T12:00:00Z",
    "eligible_from_week": "",
    "elevation_count_season": "",
    "confidence": "official",
    "is_void": "false",
    "void_reason": "",
    "supersedes": "",
    "source_ref": "wire/1",
}


def test_the_vocabulary_is_the_phase_docs_thirteen():
    assert len(TRANSACTION_TYPES) == 13
    assert "ps_elevation" in TRANSACTION_TYPES
    assert "ir_designated_return" in TRANSACTION_TYPES
    assert CONFIDENCE_LEVELS == {"reported", "official"}


def test_an_unrecognised_type_is_refused_not_bucketed():
    """Bucketing an unknown verb into the nearest known one is how a
    `ps_elevation` becomes a `signing` with nothing erroring."""
    with pytest.raises(UnknownTransactionType):
        normalize(BASE | {"transaction_type": "practice_squad_elevation"})


def test_an_unrecognised_type_classifies_as_malformed():
    """`CollectorMetrics.reason_for` keys off ValueError, so the subclassing is
    load-bearing rather than decorative."""
    from collector_core.metrics import CollectorMetrics

    assert CollectorMetrics.reason_for(UnknownTransactionType("x")) == "malformed"
    assert CollectorMetrics.reason_for(TransactionSchemaError("x")) == "malformed"


def test_the_id_is_derived_from_content_not_the_upstream_id():
    """The same move under two vendor ids must land on one key."""
    row_a = normalize(BASE | {"source_ref": "espn/99"})
    row_b = normalize(BASE | {"source_ref": "sleeper/12345"})
    assert row_a["transaction_id"] == row_b["transaction_id"]
    assert row_a["source_ref"] != row_b["source_ref"]


def test_the_id_changes_when_the_move_changes():
    row = normalize(BASE)
    moved = normalize(BASE | {"effective_at": "2026-09-03T12:00:00Z"})
    other_team = normalize(BASE | {"to_team": "buf"})
    assert len({row["transaction_id"], moved["transaction_id"]}) == 2
    assert len({row["transaction_id"], other_team["transaction_id"]}) == 2


def test_the_id_separator_cannot_be_forged():
    """`("a|b", "c")` and `("a", "b|c")` must not collide."""
    at = datetime(2026, 9, 1, tzinfo=UTC)
    assert transaction_id("a", "signing", at, "KC") != transaction_id(
        "a", "signing", at, "KC-extra"
    )
    assert transaction_id("ab", "signing", at, "") != transaction_id(
        "a", "bsigning", at, ""
    )


def test_announced_and_effective_stay_separate():
    """The gap between them is exactly the window in which every depth chart in
    the platform is wrong. Collapsing them erases the signal."""
    row = normalize(BASE)
    assert row["announced_at"] == "2026-09-01T12:00:00Z"
    assert row["effective_at"] == "2026-09-02T12:00:00Z"
    assert row["announced_at"] != row["effective_at"]


def test_an_absent_team_is_null_not_empty_string():
    """An empty string validates against a `string` schema and reads downstream
    as a team code nobody recognises."""
    row = normalize(BASE)
    assert row["from_team"] is None
    assert row["to_team"] == "KC", "team codes are normalized upper-case"


@pytest.mark.parametrize(
    "field",
    ["player_id", "announced_at", "effective_at", "confidence"],
)
def test_a_required_field_that_is_empty_fails_loudly(field):
    with pytest.raises(TransactionSchemaError):
        normalize(BASE | {field: ""})


def test_a_naive_timestamp_is_refused():
    """Guessing a timezone puts a wrong instant into an append-only lake, and
    for this collector the instant IS the signal."""
    with pytest.raises(TransactionSchemaError):
        parse_timestamp("2026-09-01T12:00:00", "announced_at")


def test_an_unparseable_timestamp_is_refused():
    with pytest.raises(TransactionSchemaError):
        parse_timestamp("last tuesday", "announced_at")


def test_a_void_row_without_a_reason_is_refused():
    """An append-only lake cannot delete a rescinded move, so the retraction row
    is the only record of why. An unexplained void is not usable."""
    with pytest.raises(TransactionSchemaError):
        normalize(BASE | {"is_void": "true", "void_reason": ""})
    row = normalize(BASE | {"is_void": "true", "void_reason": "trade voided"})
    assert row["is_void"] is True
    assert row["void_reason"] == "trade voided"


def test_a_half_populated_return_window_is_refused():
    """A window with one end lets a consumer compute a deadline from a missing
    value."""
    with pytest.raises(TransactionSchemaError):
        normalize(BASE | {"return_window_opens_at": "2026-10-01T00:00:00Z"})
    assert normalize(BASE)["return_window"] is None


def test_a_non_integer_count_is_refused_not_dropped():
    with pytest.raises(TransactionSchemaError):
        normalize(BASE | {"elevation_count_season": "three"})


def test_duplicate_signings_catch_the_reported_then_official_pair():
    """The phase doc's named failure mode: one move, two rows, different
    `effective_at`, and the generator counts two signings."""
    reported = normalize(
        BASE | {"confidence": "reported", "effective_at": "2026-09-02T12:00:00Z"}
    )
    official = normalize(
        BASE | {"confidence": "official", "effective_at": "2026-09-03T12:00:00Z"}
    )
    assert reported["transaction_id"] != official["transaction_id"], (
        "plain deduplication cannot see this — that is why it needs a "
        "reconciliation check"
    )
    assert duplicate_signing_count([reported, official]) == 1


def test_an_intervening_departure_makes_a_second_signing_legitimate():
    first = normalize(BASE)
    release = normalize(
        BASE
        | {
            "transaction_type": "release",
            "from_team": "kc",
            "to_team": "",
            "effective_at": "2026-09-03T12:00:00Z",
        }
    )
    resigned = normalize(BASE | {"effective_at": "2026-09-04T12:00:00Z"})
    assert duplicate_signing_count([first, release, resigned]) == 0


def test_two_signings_outside_the_window_are_not_duplicates():
    first = normalize(BASE)
    later = normalize(BASE | {"effective_at": "2026-09-10T12:00:00Z"})
    assert duplicate_signing_count([first, later]) == 0


def test_a_quiet_pass_counts_zero_duplicates_rather_than_nothing():
    """Recorded on every pass, including zero: an absent Prometheus series and
    a healthy one are indistinguishable in PromQL."""
    assert duplicate_signing_count([]) == 0


def test_a_voided_row_does_not_count_as_a_signing():
    first = normalize(BASE)
    voided = normalize(
        BASE
        | {
            "effective_at": "2026-09-02T18:00:00Z",
            "is_void": "true",
            "void_reason": "failed physical",
        }
    )
    assert duplicate_signing_count([first, voided]) == 0


def test_a_release_is_not_itself_a_signing():
    release = normalize(
        BASE | {"transaction_type": "release", "from_team": "kc", "to_team": ""}
    )
    assert release["to_team"] is None
    assert duplicate_signing_count([release, release]) == 0


def test_timestamps_normalize_to_utc():
    parsed = parse_timestamp("2026-09-01T08:00:00-04:00", "announced_at")
    assert parsed == datetime(2026, 9, 1, 12, tzinfo=UTC)
    assert parsed.utcoffset() == timedelta(0)
