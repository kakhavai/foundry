"""The spec's named failure mode, made measurable.

> a listed weight or position that silently goes stale is invisible — a tight
> end converted to fullback, or a back who added fifteen pounds in an offseason,
> keeps producing well-formed records with the old value, and
> `position_age_curve_stage` then places them on the wrong curve entirely.
> Nothing errors; the model just quietly uses the wrong prior. The guard is a
> per-field `last_changed_at` on `position` and `weight_lbs` plus an assertion
> that fires when a scoped player's `position` has not been re-confirmed by any
> adapter within 45 days during the season — **staleness must be a measured age,
> not an assumption.**

Two timestamps per tracked field, and conflating them is the whole trap:

`last_changed_at`
    When this collector first observed the value it is publishing now. `null`
    until a change has actually been observed — see `TRACKED_FIELDS` below for
    why that null is honest rather than missing.

`last_confirmed_at`
    When an adapter last **re-asserted** the value from a freshly served
    upstream document. This is the one the 45-day assertion runs on, because it
    is the one the spec asks for ("re-confirmed by any adapter").

**What makes it a measurement rather than an assumption is conditional GET.**
`last_confirmed_at` advances only on a pass where the upstream actually
re-served the document — `ProfileTable.republished` — and not on a pass that
was answered `304`. A `304` means nobody republished the listed weight, so
nothing about it was re-confirmed, and reading a cached copy back must not
reset the clock. Take conditional GET out of the adapter and every pass looks
like a re-confirmation, the age never exceeds one capture interval, and the
assertion can never fire: the guard would still be there, still be green, and
mean nothing.

**Prior state comes from this collector's own last lake object**, the same
shape `player-stats` uses for its revision counter. In-memory alone would reset
every deploy, which for a `static reference` collector is the difference
between "this weight has not been re-confirmed in 51 days" and a permanent
"we have no idea".
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from collector_core.lake import alist_keys, aread

logger = logging.getLogger(__name__)

__all__ = [
    "IN_SEASON_WEEKS",
    "STALE_AFTER_DAYS",
    "TRACKED_FIELDS",
    "FieldHistory",
    "advance",
    "in_season",
    "is_stale",
    "load_prior_histories",
    "rfc3339",
]

# The two fields the spec names. Not "every field": a `last_changed_at` pair on
# all seventeen would triple the row size to guard two values, and the other
# fifteen either cannot change (`birth_date`, the draft block) or change in a
# way that is self-evidently visible (`age_years` moves every single day).
TRACKED_FIELDS = ("position", "weight_lbs")

# The spec's number, verbatim.
STALE_AFTER_DAYS = 45

# "during the season". Weeks 1–18 regular plus the four postseason rounds, which
# is the window `roster-scope` publishes a membership list for at all. Outside
# it a 45-day-old listed weight is ordinary rather than alarming — the league is
# not playing — so the assertion is scoped rather than firing every August.
IN_SEASON_WEEKS = range(1, 23)

BIOGRAPHICAL_SIGNAL = "player_biographical"


def rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        # A timestamp this collector cannot parse is treated as absent rather
        # than raising: the value came out of an append-only lake object that
        # may predate a format change, and one unparseable cell must not fail a
        # whole pass. It reads as "never confirmed", which suppresses the
        # assertion rather than firing it falsely.
        return None


@dataclass(frozen=True)
class FieldHistory:
    """One tracked field's published value and its two ages."""

    value: object
    last_changed_at: str | None
    last_confirmed_at: str | None


def in_season(week: int) -> bool:
    return week in IN_SEASON_WEEKS


def advance(
    prior: FieldHistory | None,
    value: object,
    *,
    now: datetime,
    republished: bool,
) -> FieldHistory:
    """This pass's history for one field, from the prior pass's.

    Four cases, and the third is the one that carries the guard:

    * no prior — first observation. `last_changed_at` is `null`, because
      nothing has been observed to change yet and stamping `now` would claim a
      change this collector did not see. `last_confirmed_at` is `now` only if
      the document was actually served.
    * prior, value differs — a real change. Both stamps advance.
    * prior, value identical, document republished — re-confirmed. The change
      stamp is carried forward untouched, the confirmation stamp advances. This
      is what stops a value that never changes from looking stale forever.
    * prior, value identical, `304` — **nothing happened.** Both stamps are
      carried forward, so the age of `last_confirmed_at` grows, which is
      precisely the measurement the assertion needs.
    """
    confirmed = rfc3339(now) if republished else None
    if prior is None:
        return FieldHistory(
            value=value, last_changed_at=None, last_confirmed_at=confirmed
        )
    if prior.value != value:
        return FieldHistory(
            value=value,
            last_changed_at=rfc3339(now),
            last_confirmed_at=confirmed or prior.last_confirmed_at,
        )
    return FieldHistory(
        value=value,
        last_changed_at=prior.last_changed_at,
        last_confirmed_at=confirmed or prior.last_confirmed_at,
    )


def is_stale(
    history: FieldHistory,
    *,
    now: datetime,
    week: int,
    stale_after_days: int = STALE_AFTER_DAYS,
) -> bool:
    """Whether this field has gone un-re-confirmed past the threshold.

    **`False` when `last_confirmed_at` is `null`**, and that is the deliberate
    reading of "staleness must be a measured age, not an assumption": a value
    this collector has never seen confirmed has an *unknown* age, not an
    infinite one. Treating unknown as stale would make every first pass of every
    new deploy fire the assertion for the entire scope, which is the fastest
    known way to get an alert switched off. The absence shows up instead as a
    null in the published row, which a consumer can see.
    """
    if not in_season(week):
        return False
    confirmed = _parse(history.last_confirmed_at)
    if confirmed is None:
        return False
    return (now - confirmed) > timedelta(days=stale_after_days)


def _histories_from_row(row: dict) -> dict[str, FieldHistory]:
    return {
        field: FieldHistory(
            value=row.get(field),
            last_changed_at=row.get(f"{field}_last_changed_at"),
            last_confirmed_at=row.get(f"{field}_last_confirmed_at"),
        )
        for field in TRACKED_FIELDS
    }


async def load_prior_histories(
    lake, season: int, week: int
) -> dict[str, dict[str, FieldHistory]]:
    """`player_id -> field -> FieldHistory`, from the newest published envelope.

    Falls back to `week - 1` for the same reason `ScopeClient` does: this
    collector's cadence is daily and `roster-scope`'s is weekly, so the first
    pass after a rollover would otherwise find no prior at all and reset every
    player's history to "first observation" — silently discarding the very ages
    the guard exists to accumulate.

    **Never raises.** A lake that cannot be read costs this pass its history,
    which degrades the guard to "unknown" (see `is_stale`); it must not fail a
    capture that is otherwise fine, because the profile rows themselves do not
    depend on it. The failure is logged and the pass records it in `errors`.
    """
    for candidate_week in (week, week - 1):
        if candidate_week < 1:
            continue
        try:
            keys = await alist_keys(
                lake, "player-profile", BIOGRAPHICAL_SIGNAL, season, candidate_week
            )
            if not keys:
                continue
            # `list_keys` returns captured_at order, so the newest is last.
            envelope = await aread(lake, keys[-1])
        except Exception:  # noqa: BLE001 — history is best-effort; see docstring
            logger.exception(
                "player-profile: could not read prior histories for %s week %s",
                season,
                candidate_week,
            )
            return {}
        return {
            row["player_id"]: _histories_from_row(row)
            for row in envelope.get("signals", [])
            if row.get("player_id")
        }
    return {}
