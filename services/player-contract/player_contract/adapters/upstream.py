"""The upstream adapter — the only module that knows the wire format.

One feed: nflverse's `contracts` release, sourced from OverTheCap, read as
**parquet**. Measured 2026-08-01: 6.44 MiB on the wire, 51,785 rows, 25 arrow
fields (37 physical leaves — one is a nested list-of-struct), 2,931 active.

--------------------------------------------------------------------------
Why parquet here, against the fleet's CSV-over-parquet rule
--------------------------------------------------------------------------

`docs/collectors.md` says take `.csv.gz` where nflverse publishes one and do
not add `pyarrow` to the fleet. That rule is a **size** argument, decided on the
play-by-play feed where the parquet (19.40 MiB) is *larger* than the gzipped CSV
(18.22 MiB). It does not govern this release, and the reason is not size:

| asset | last updated |
|---|---|
| `historical_contracts.csv.gz` | **2022-05-29** |
| `historical_contracts.parquet` | 2026-08-01 09:11 |
| `historical_contracts.rds` | 2026-08-01 09:11 |
| `timestamp.json` | `2026-08-01 05:11:43 EDT` |

nflverse rebuilds this release daily and **stopped regenerating its CSV
artifact four years ago**. The documents agree with their own timestamps: the
CSV's newest `year_signed` is 2022 and 2,869 of its 2,887 "active" contracts had
already expired; the parquet's newest is 2026, with 1,793 of 2,931 active deals
signed this year. So the choice was between a live document and a dead one.

Two things keep this from being a fleet-wide precedent. `pyproject.toml` is
per-service, so the 26.7 MiB cp312 wheel lands in this image only. And parquet's
footer-at-the-end layout — the real objection, because it forces the body to be
buffered before a single row can be read — costs 6.44 MiB here rather than the
93 MiB that made it unacceptable for play-by-play. The rule is amended in
`docs/collectors.md` rather than quietly broken: **compare `updated_at` across
formats before choosing one, not just size.**

--------------------------------------------------------------------------
MONEY IS IN MILLIONS. The CSV's was in whole dollars.
--------------------------------------------------------------------------

This is the single most dangerous difference between the two artifacts, because
both are numerically plausible and neither raises anything.

    CSV      value = 150815000      (whole USD, integer)
    parquet  value = 448.0          (MILLIONS, double — Mahomes, $448M)

`value`, `apy` and `guaranteed` are doubles denominated in millions, as is every
money field inside the nested per-season struct. Publishing `total_value_usd:
448` for a $448M contract is a well-formed record wrong by six orders of
magnitude, and it is wrong for *every* row at once, which is exactly the shape
that survives a spot check.

`_usd` converts. Verified against all 2,931 active rows and all 2,219 populated
2026 cap entries: **every value lands exactly on a whole dollar after x1,000,000**
(zero residuals above 1e-3), so the feed's precision is whole dollars expressed
in millions and the conversion is lossless rather than a rounding convention.

`apy` is still **absent from `COLUMNS`** rather than read and ignored: it is
average annual value, a cap hit is not, and the two agreeing on some rows is
what makes substituting one for the other dangerous. A field this module never
reads cannot be substituted by a later edit that looked reasonable in isolation.

`inflated_value` / `inflated_apy` / `inflated_guaranteed` are still not read, and
the live parquet confirms exactly why: they equal `value` on the 1,793 deals
signed in 2026 and diverge on the 1,138 older ones (Burrow, signed 2023: value
275.0, inflated 368.46). That is OverTheCap restating against a cap-inflation
index this collector cannot see, version or reproduce — an index that moves, so
the same historical contract yields a different number next season and an
append-only lake object stops being reproducible from its own `captured_at`.

--------------------------------------------------------------------------
The parquet carries a per-season cap table the CSV did not
--------------------------------------------------------------------------

The CSV's `season_history` column — empty on all 31,893 rows — is **gone**,
replaced by `cols`: a list of per-season structs carrying `year`, `team`,
`base_salary`, `prorated_bonus`, `roster_bonus`, `guaranteed_salary`,
`cap_number`, `cap_percent`, `cash_paid`, `workout_bonus`, `other_bonus`,
`per_game_roster_bonus` and `option_bonus`.

It is populated: 2,250 of 2,931 active rows carry one, and for the 2026 season
`cap_number` is present on 2,219 rows (75.7% of active) and `prorated_bonus` on
1,754 (59.8%). No active row has two entries for the same year, so keying by
year is safe.

That makes **two of the six "null by necessity" fields sourceable** —
`cap_hit_current_usd` from `cap_number` and `signing_bonus_proration_usd` from
`prorated_bonus`, both direct lookups requiring no derivation. See
`capture.build_signal` for what is still null and why.

--------------------------------------------------------------------------
`gsis_id` — a Tier-1 crosswalk key the CSV did not have
--------------------------------------------------------------------------

The parquet carries `gsis_id` on 2,251 of 2,931 active rows (76.8%). `gsis` is a
**published crosswalk source** in `player_identity.identity.CROSSWALK_KEYS`,
adopted at Tier 1 with no attribute scoring at all — so three quarters of this
collector's rows now join the way every other scope-aware collector in the fleet
joins, instead of falling into Tier 3 weighted name agreement. `adapters/scope.py`
owns the two query shapes that follow from it.

--------------------------------------------------------------------------
Memory: filter as you parse, even without a row stream
--------------------------------------------------------------------------

Parquet cannot be streamed off the wire — the footer is at the end. The body is
buffered once (6.44 MiB), then read through `iter_batches` with **column
projection**, and each batch is **filtered in Arrow before anything becomes a
Python object**. Column projection is native and cheaper here than it was for
CSV: an unread column is never decompressed at all.

That last step is not a micro-optimisation, and it is the one place this format
punishes a careless read. 48,854 of the 51,785 rows are historical, and
`RecordBatch.to_pylist()` builds a dict — plus a list of nested cap-table dicts
— for *every* row in the batch before a single `is_active` can be inspected.
Measured end to end against the live document, on a 268 MB (256Mi) pod limit:

    filter after  to_pylist()   peak RSS 157.6 MB   (110.8 MB headroom)
    filter before to_pylist()   peak RSS 121.7 MB   (146.7 MB headroom)

Same rows, same answer, 36 MB apart. `roster-scope` was OOMKilled for the
equivalent mistake on a CSV feed, and neither its 171 passing tests nor a local
`docker run` could see it, because neither had a memory limit.

Note the floor: ~50 MB of that peak is baseline RSS for the interpreter with
`pyarrow` imported, which is the standing cost of this dependency and is why it
is scoped to this one service rather than the fleet.

Never hold the response more than once. `response.aread()` into one `bytes`, one
reader over it, and nothing else.
"""

import io
from dataclasses import dataclass

import httpx
import pyarrow.compute as pc
import pyarrow.parquet as pq
from collector_core.conditional import ETAGS, ETagStore, conditional_stream

__all__ = [
    "COLUMNS",
    "OTC_POSITIONS",
    "OTC_TEAM_NICKNAMES",
    "REQUIRED_COLUMNS",
    "UPSTREAM_ADAPTER",
    "UPSTREAM_URL",
    "ContractRow",
    "ContractsRead",
    "canonical_position",
    "canonical_team",
    "fetch_active_contracts",
    "source_ref",
    "to_usd",
]

# Names the upstream in every envelope's `upstream.adapter`. It says `parquet`
# on purpose: the CSV artifact of the same release is a different document with
# different units and four-year-old contents, and a consumer reading two lake
# objects a season apart must be able to tell which one it has.
UPSTREAM_ADAPTER = "nflverse-otc-contracts-parquet"

UPSTREAM_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "contracts/historical_contracts.parquet"
)

# Asserted against the document's schema before a single row is mapped, so a
# renamed column fails the pass loudly rather than writing nulls into an
# append-only lake that is never rewritten.
REQUIRED_COLUMNS = frozenset(
    {
        "player",
        "position",
        "team",
        "is_active",
        "year_signed",
        "years",
        "value",
        "guaranteed",
        "otc_id",
        "gsis_id",
        "cols",
    }
)

# The projection. The document carries 25 fields and this collector reads
# eleven; in parquet an unread column is never decompressed at all, so this is a
# larger saving than the equivalent CSV projection was. `apy` and the three
# `inflated_*` columns are excluded deliberately — see the module docstring.
COLUMNS = REQUIRED_COLUMNS

# Rows per `iter_batches` step. Bounds how much of the nested `cols` column is
# materialised at once: the whole column across 51,785 rows is the only part of
# this document large enough to matter.
BATCH_ROWS = 4096

# The feed's own null spellings for the string columns, alongside the empty
# string. The parquet types most columns natively, so this is narrower than the
# CSV's equivalent — but `gsis_id` and `date_of_birth` still carry `"NA"`.
NULL_STRINGS = frozenset({"", "NA", "N/A", "NULL", "NAN", "NONE"})

# Money in this document is denominated in MILLIONS. See the module docstring:
# verified lossless across every active row and every populated 2026 cap entry.
USD_PER_UNIT = 1_000_000

# The nested per-season cap table, and the two members read from it.
SEASON_TABLE = "cols"
SEASON_YEAR = "year"
CAP_NUMBER = "cap_number"
PRORATED_BONUS = "prorated_bonus"

# OverTheCap club nicknames -> the canonical abbreviations `roster-scope`,
# `player-identity` and the rest of the fleet use (`roster_scope.rules.TEAMS`).
# All 32 appear in the active set; an unrecognised label maps to `None` rather
# than being passed through, because a wrong team scores as disagreement at
# `player-identity` while an absent one simply does not contribute.
OTC_TEAM_NICKNAMES: dict[str, str] = {
    "Cardinals": "ARI",
    "Falcons": "ATL",
    "Ravens": "BAL",
    "Bills": "BUF",
    "Panthers": "CAR",
    "Bears": "CHI",
    "Bengals": "CIN",
    "Browns": "CLE",
    "Cowboys": "DAL",
    "Broncos": "DEN",
    "Lions": "DET",
    "Packers": "GB",
    "Texans": "HOU",
    "Colts": "IND",
    "Jaguars": "JAX",
    "Chiefs": "KC",
    "Chargers": "LAC",
    "Rams": "LAR",
    "Raiders": "LV",
    "Dolphins": "MIA",
    "Vikings": "MIN",
    "Patriots": "NE",
    "Saints": "NO",
    "Giants": "NYG",
    "Jets": "NYJ",
    "Eagles": "PHI",
    "Steelers": "PIT",
    "Seahawks": "SEA",
    "49ers": "SF",
    "Buccaneers": "TB",
    "Titans": "TEN",
    "Commanders": "WAS",
}

# OverTheCap position codes -> `player-identity`'s `KNOWN_POSITIONS`
# (`player_identity.identity._POSITION_GROUPS`).
#
# **This mapping is a hard requirement, not a nicety.** `player-identity`'s
# `build_query` raises 422 on a position it does not know, and `/resolve/batch`
# validates the whole body — so ONE `ED` in a chunk of 500 fails all 500, which
# `IdentityClient` records as an `identity_upstream_error` against every query
# in it. Six of OverTheCap's eighteen codes (`ED`, `IDL`, `LT`, `RT`, `LG`,
# `RG`) are absent from that set and they cover **984 of the 2,931 active rows**
# in the live parquet.
#
# An unrecognised code therefore maps to `None` — one row resolving on its other
# attributes — rather than being passed through to 422 the batch it travels in.
# `tests/test_upstream_adapter.py` reads `player-identity`'s own dictionary and
# fails if a value here ever drifts out of it.
OTC_POSITIONS: dict[str, str] = {
    "QB": "QB",
    "RB": "RB",
    "FB": "FB",
    "WR": "WR",
    "TE": "TE",
    "LT": "OT",
    "RT": "OT",
    "LG": "G",
    "RG": "G",
    "C": "C",
    "IDL": "DT",
    "ED": "DE",
    "LB": "LB",
    "CB": "CB",
    "S": "S",
    "K": "K",
    "P": "P",
    "LS": "LS",
}


class UpstreamSchemaError(ValueError):
    """The document did not carry the columns this collector depends on.

    Subclasses `ValueError` so `CollectorMetrics.reason_for` classifies it as
    `malformed`, matching `collector_core.streaming`'s type of the same name —
    which cannot be reused here, because nothing in this adapter goes through
    `stream_csv_dicts`. An upstream that renames a field must fail the capture
    loudly rather than map nulls into an append-only lake.
    """


@dataclass(frozen=True)
class ContractRow:
    """One active contract, in this collector's own row shape.

    Every money member is **whole USD**, converted from the document's millions
    by `to_usd`. Nothing downstream of this dataclass knows the wire unit, which
    is the point: the conversion happens once, here, next to the only docstring
    that explains it.

    The nullable members are nullable because a blank is a *fact about the row* —
    "the upstream did not say" — and dropping the row over one absent term would
    discard the team, the value and the identity that are present. `capture.py`
    keeps that null distinct from the fields the SOURCE never carries.
    """

    otc_id: str
    gsis_id: str | None
    player_name: str
    otc_position: str
    otc_team: str
    year_signed: int | None
    years: int | None
    total_value_usd: int | None
    guaranteed_total_usd: int | None
    # From the nested per-season table, for the season being captured. Null when
    # the row has no entry for that season — which is a different fact from the
    # source not carrying cap accounting at all.
    cap_hit_current_usd: int | None
    signing_bonus_proration_usd: int | None


@dataclass(frozen=True)
class ContractsRead:
    """One pass's kept rows, plus what the document cost to get them.

    `malformed` and `duplicate_active` are returned rather than logged because
    `capture.py` files them in the envelope's `errors`. A row this adapter
    silently discarded would otherwise show up only as a scope slot with no
    contract, which is the same symptom as a player who genuinely has none.
    """

    rows: list[ContractRow]
    malformed: int
    duplicate_active: int


def source_ref(season: int, week: int) -> str:
    """The exact upstream artifact this pass read, recorded in the envelope.

    Constant: the feed is a single all-history document with no season or week
    in its path. It is still recorded, because a lake object a season later must
    say which of two feeds a row came from — and this release publishes two
    documents under one name that disagree by four years.
    """
    return UPSTREAM_URL


def to_usd(value) -> int | None:
    """A money field in the document's millions, as whole US dollars.

    `round`, not `int`: `int()` truncates, and 146.51 x 1e6 is 146509999.99...
    in binary floating point, so truncation would quietly shave a dollar off a
    majority of the league. Verified across every active row that the rounded
    result is within 1e-3 of the exact product, so this is a representation fix
    rather than a precision convention.
    """
    if value is None:
        return None
    try:
        return round(float(value) * USD_PER_UNIT)
    except (TypeError, ValueError):
        return None


def canonical_team(raw: str) -> str | None:
    """An OverTheCap club label as a canonical abbreviation, or `None`.

    `None` for a multi-club string (64 active rows) and for anything
    unrecognised. A wrong team is worse than an absent one at both ends: `team`
    carries 0.20 of `player-identity`'s resolution weight and scores as
    *disagreement*, and publishing it names a club the player does not play for.

    The multi-club rows are not guessable. The ordering is inconsistent —
    `PHI/IND/WAS` (Wentz) is chronological while `IND/ATL` (Ryan) is reversed —
    so neither the first nor the last segment is reliably the current club.
    """
    label = (raw or "").strip()
    if not label or "/" in label:
        return None
    return OTC_TEAM_NICKNAMES.get(label)


def canonical_position(raw: str) -> str | None:
    """An OverTheCap position code as one `player-identity` knows, or `None`.

    `None` rather than the raw code for an unrecognised one. Passing it through
    422s the entire 500-query batch it travels in — see `OTC_POSITIONS`.
    """
    return OTC_POSITIONS.get((raw or "").strip().upper())


def _text(value) -> str:
    """A string column's value, normalised, with the feed's nulls as empty."""
    text = "" if value is None else str(value).strip()
    return "" if text.upper() in NULL_STRINGS else text


def _int_or_none(value) -> int | None:
    """A whole-count column (a season, a term) as an int.

    Distinct from `to_usd`: these are counts, not money, and are not scaled.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number) if abs(number - round(number)) < 1e-6 else None


def _season_entry(row: dict, season: int) -> dict:
    """The nested per-season cap entry for `season`, or an empty dict.

    **Matched on the year, exactly, and never by position in the list.** Two
    separate traps make that non-negotiable:

    1. The list is a *career* table running from the player's rookie season, so
       indexing it reads a different season for every player.
    2. **Its last element is not a season.** Every one of the 2,250 active rows
       carrying a cap table ends with a pseudo-row whose `year` is the literal
       string `"Total"` and whose `cap_number` is the CAREER total — Joe
       Burrow's is $339,443,060 against a 2026 cap hit an order of magnitude
       smaller. So `cols[-1]`, the obvious "give me the latest" shortcut,
       publishes a career total as a current-season cap hit for every player in
       the league, at a magnitude plausible enough to survive a spot check.

    `year` arrives as a **string** even though every other season-ish column in
    the document is an int32, so it is compared as text — which is also what
    makes `"Total"` fall out for free rather than needing its own exclusion.

    Verified against the live document that no active row carries two entries
    for one year, so the first exact match is the only match.
    """
    for entry in row.get(SEASON_TABLE) or ():
        if entry and str(entry.get(SEASON_YEAR)) == str(season):
            return entry
    return {}


def _to_row(raw: dict, season: int) -> ContractRow | None:
    """One document row as a `ContractRow`, or `None` if it cannot be one.

    Only a blank `player` disqualifies a row outright: the name is the fallback
    join key for the 23% of rows carrying no `gsis_id`, and a row with neither
    is one `player-identity` could not resolve by any route. Everything else
    degrades to a null field rather than costing the row.
    """
    name = _text(raw.get("player"))
    if not name:
        return None
    years = _int_or_none(raw.get("years"))
    season_entry = _season_entry(raw, season)
    return ContractRow(
        # int32 in this document, string in the CSV. Normalised to text here so
        # the published `otc_player_id` has one type regardless of the artifact.
        otc_id=_text(raw.get("otc_id")),
        gsis_id=_text(raw.get("gsis_id")) or None,
        player_name=name,
        otc_position=_text(raw.get("position")),
        otc_team=_text(raw.get("team")),
        year_signed=_int_or_none(raw.get("year_signed")),
        # A zero- or negative-length contract is not a term, it is a bad cell.
        # Left null so `contract_end_season` is null rather than the season
        # BEFORE the deal was signed, which is a well-formed lie.
        years=years if years is not None and years >= 1 else None,
        total_value_usd=to_usd(raw.get("value")),
        guaranteed_total_usd=to_usd(raw.get("guaranteed")),
        cap_hit_current_usd=to_usd(season_entry.get(CAP_NUMBER)),
        signing_bonus_proration_usd=to_usd(season_entry.get(PRORATED_BONUS)),
    )


def _sort_key(row: ContractRow) -> tuple:
    """Total order over the active rows sharing one `otc_id`; smallest wins.

    Newest signing first, then the longer deal, then the larger value, then the
    position code — the last purely to break a remaining tie the same way every
    time. Determinism is the whole requirement: without a total order the
    published row would depend on the order the document arrived in and would
    flip between passes for no reason a reader could see.
    """
    return (
        -(row.year_signed if row.year_signed is not None else -1),
        -(row.years if row.years is not None else -1),
        -(row.total_value_usd if row.total_value_usd is not None else -1),
        row.otc_position,
    )


def _dedupe(rows: list[ContractRow]) -> tuple[list[ContractRow], int]:
    """One row per player, plus how many active rows were discarded.

    Keyed on `otc_id` and falling back to `(name, position)` when the feed omits
    one — never on the name alone. Twelve distinct names appear twice in the
    live active set (a QB and an edge rusher both called Josh Allen among them),
    and collapsing those would silently delete a player.
    """
    best: dict[tuple[str, str], ContractRow] = {}
    discarded = 0
    for row in rows:
        key = (
            ("otc", row.otc_id)
            if row.otc_id
            else ("name", f"{row.player_name}|{row.otc_position}")
        )
        current = best.get(key)
        if current is None:
            best[key] = row
            continue
        discarded += 1
        if _sort_key(row) < _sort_key(current):
            best[key] = row
    return list(best.values()), discarded


def _validate_schema(names) -> None:
    missing = set(REQUIRED_COLUMNS) - set(names)
    if missing:
        raise UpstreamSchemaError(
            f"{UPSTREAM_URL} is missing column(s): {', '.join(sorted(missing))}"
        )


async def fetch_active_contracts(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    etag_store: ETagStore = ETAGS,
) -> ContractsRead:
    """Every active contract in the feed, one row per player.

    Conditional GET via `collector_core.conditional` — the release asset serves
    an `ETag` and answers `If-None-Match` with a `304` carrying zero bytes
    (verified against the live endpoint on 2026-08-01; a ranged control request
    returns `206`, so the 304 is caused by the header rather than a dead URL).
    This is `docs/collectors.md`'s **Route 2**: the adapter reads the response
    itself rather than going through `stream_csv_dicts`, so it must call
    `stream.commit()` as its last statement — after the body is complete and
    after every check on it has passed. An ETag committed for a partial read
    turns a loud, self-retrying failure into a silent, sticky one.

    Raises on an upstream failure rather than returning an empty read. That is
    deliberate: `capture` turns the exception into a `present: 0` envelope with
    a populated `errors` array, while an empty list would be recorded as a
    successful capture of a league in which nobody is under contract.

    `UpstreamUnchanged` escapes on a `304` and `capture` re-raises it above its
    generic handler — a 304 is a **successful** capture, and routing it into
    `fail_capture` would write `present: 0` over healthy data.
    """
    kept: list[ContractRow] = []
    malformed = 0

    async with conditional_stream(
        client, UPSTREAM_URL, etag_key=UPSTREAM_URL, etag_store=etag_store
    ) as stream:
        # Parquet's footer is at the end, so the body cannot be consumed
        # incrementally the way a CSV can. Read once into one buffer — never
        # `aread()` followed by `.content`, which is two copies of 6.44 MiB.
        body = await stream.response.aread()

        parquet = pq.ParquetFile(io.BufferedReader(io.BytesIO(body)))
        _validate_schema(parquet.schema_arrow.names)

        # Projected and batched: the nested `cols` column is the only part of
        # this document big enough to matter, and this materialises it
        # BATCH_ROWS at a time instead of across all 51,785 rows.
        for batch in parquet.iter_batches(
            batch_size=BATCH_ROWS, columns=sorted(COLUMNS)
        ):
            # **Filtered in Arrow, before anything becomes a Python object.**
            # 48,854 of 51,785 rows are historical, and `to_pylist()` on a whole
            # batch builds a dict — and a list of nested cap-table dicts — for
            # every one of them before a single `is_active` is inspected.
            # Measured on the live document: filtering after cost 157.6 MB peak
            # RSS against a 268 MB pod limit, filtering here costs far less, for
            # the same rows. This is the length half of the memory rule applied
            # where the format actually allows it.
            #
            # `fill_null(False)` is the semantic half: `is_active` is a NULLABLE
            # bool, and a null flag is not a claim that the contract is current.
            # `filter` drops nulls by default, but saying so is what keeps that
            # a decision rather than a default nobody checked.
            mask = pc.fill_null(batch.column("is_active"), False)
            for raw in batch.filter(mask).to_pylist():
                row = _to_row(raw, season)
                if row is None:
                    malformed += 1
                    continue
                kept.append(row)

        # The last statement inside the context, after the whole body is read
        # and the schema has been checked. Any earlier exit — a short read, a
        # renamed column — never reaches it, so the next pass re-downloads
        # unconditionally.
        stream.commit()

    rows, duplicate_active = _dedupe(kept)
    return ContractsRead(
        rows=rows, malformed=malformed, duplicate_active=duplicate_active
    )
