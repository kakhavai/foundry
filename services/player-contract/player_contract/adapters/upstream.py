"""The upstream adapter — the only module that knows the wire format.

One feed: nflverse's `contracts` release, sourced from OverTheCap.

Measured 2026-08-01: **1.13 MiB** on the wire, 5.79 MiB inflated, 31,893 rows
of which 2,908 are flagged active.

Read through `collector_core.streaming.stream_csv_dicts` with `gzipped=True`,
`columns=` and conditional GET on, filtering to active contracts **as the rows
parse**. 31,893 rows in, ~2,908 kept: materialising the other 28,985 first is
exactly the waste that OOMKilled `roster-scope` on its first deploy.

`gzipped=True` is not only bandwidth. A gzip member carries a trailer, so a
short body raises `UpstreamTruncated` instead of yielding half a document —
and half of *this* document is a plausible-looking league in which two thirds
of the players have no contract. `MAX_INFLATED_CHUNK` bounds peak memory
independently of whatever chunk size the transport picks. Neither is optional;
see `collector_core.streaming`.

--------------------------------------------------------------------------
The `.csv.gz` asset is FROZEN, and that is the headline fact about this feed
--------------------------------------------------------------------------

Measured 2026-08-01 against the live release:

    historical_contracts.csv.gz   Last-Modified: Sun, 29 May 2022 07:12:13 GMT
    historical_contracts.parquet  updated 2026-08-01T09:11:45Z
    historical_contracts.rds      updated 2026-08-01T09:11:45Z
    timestamp.json                {"last_updated": "2026-08-01 05:11:43 EDT"}

The release itself is refreshed daily; the **CSV variant is not regenerated**.
The document's own contents agree: the newest `year_signed` anywhere in it is
**2022**, and `is_active` describes a 2022 roster.

So every record this collector publishes today is a contract as OverTheCap
knew it in May 2022. That is disclosed rather than papered over, in three
places a consumer cannot miss:

* `seasons_remaining` is **not clamped at zero**. A deal whose final season
  precedes the capture season yields a negative number, which is impossible to
  misread as a live contract. Clamping would have turned the staleness into a
  plausible `0`.
* `player_contract_expired_records` counts them and `capture.py` files a
  **priority** coverage error naming the count. `collector_coverage_ratio`
  cannot see this: an expired deal is a *present* record with a non-null
  `contract_end_season`, so coverage reads 1.0 while every row is four years
  old.
* `CAPTURE_ENABLED` stays `false`. See the service README.

Swapping to a live source is a change to `UPSTREAM_URL` and `_to_row`, and
nothing else. The parquet variant was considered and rejected: `docs/
collectors.md` settles the format question fleet-wide — `pyarrow` is a 47.8 MiB
wheel added to every collector image, and parquet's footer-at-the-end layout
forces the body to be buffered before a single row can be read, reversing the
streaming rule this module exists to obey.

--------------------------------------------------------------------------
Money is already whole USD. Do not re-scale it, and do not read `apy`
--------------------------------------------------------------------------

`value`, `apy` and `guaranteed` are integer dollars (Aaron Rodgers:
`value=150815000`). `apy` is deliberately **absent from `COLUMNS`** rather than
read and ignored: it is average annual value, a cap hit is not, and the two
agreeing on some rows is precisely what makes substituting one for the other
dangerous. A field this module never reads cannot be substituted by a later
edit that looked reasonable in isolation.

`inflated_value` / `inflated_apy` / `inflated_guaranteed` are OverTheCap's own
restatement into present-day dollars, computed against a cap-inflation index
this collector cannot see, cannot version and cannot reproduce. That index
moves, so the same historical contract yields a different `inflated_value` next
season — which would make an append-only lake object un-reproducible from its
own `captured_at`. The **nominal** figures are emitted and the inflated ones are
not read at all. Mixing the two silently is the failure this rules out by
construction.

--------------------------------------------------------------------------
`is_active` is UPSTREAM's flag, not ours
--------------------------------------------------------------------------

A player with three historical contracts has three rows and only one is
current. `_is_active` is the whole of "which row is the current deal", and
getting it wrong produces a well-formed record describing a deal that expired
in 2019 — indistinguishable, downstream, from a correct one.

Twenty-one `otc_id`s carry **two** active rows (measured; they differ only in
`position`, e.g. `LT` versus `RT` for the same lineman). `_dedupe` picks one
deterministically and counts the rest, so the choice cannot depend on the order
the document happens to arrive in.

--------------------------------------------------------------------------
`team` is a NICKNAME, and a multi-club string is ambiguous in both directions
--------------------------------------------------------------------------

Single-club rows carry `Packers`, `Bills`, `49ers` — all 32 nicknames appear,
none of them the canonical abbreviation the rest of the fleet uses. Sending
`PACKERS` to `player-identity` would be worse than sending nothing: `team`
carries 0.20 of the resolution weight and a wrong value scores as
*disagreement*.

61 active rows carry a slash-joined multi-club string, and the ordering is
**not consistent**: `PHI/IND/WAS` (Wentz) and `NYJ/CAR` (Darnold) are
chronological, while `DEN/SEA` (Wilson, signed by SEA, traded to DEN) and
`IND/ATL` (Ryan, signed by ATL, traded to IND) are reversed. Neither the first
nor the last segment is reliably the current club, so those rows resolve and
publish with `team: null` rather than a coin flip. That is 2.1% of the active
set.
"""

from dataclasses import dataclass

import httpx
from collector_core.streaming import stream_csv_dicts

__all__ = [
    "ACTIVE_VALUES",
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
]

# Names the upstream in every envelope's `upstream.adapter`. Change it when the
# upstream changes: it is how a consumer tells two sources of the same signal
# apart in the lake, and this one WILL change when the frozen CSV is replaced.
UPSTREAM_ADAPTER = "nflverse-otc-contracts"

UPSTREAM_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "contracts/historical_contracts.csv.gz"
)

# Asserted against the header before a single row is mapped, so a renamed
# column fails the pass loudly rather than writing nulls into an append-only
# lake that is never rewritten.
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
    }
)

# The projection. Identical to `REQUIRED_COLUMNS` on purpose: the document
# carries 24 columns and this collector emits eight sourced facts, so building
# a 24-key dict for each of 31,893 rows is allocation churn for columns nobody
# reads. `apy` and the three `inflated_*` columns are excluded deliberately —
# see the module docstring.
COLUMNS = REQUIRED_COLUMNS

# `is_active` as the feed writes it. Compared case-insensitively against this
# set rather than by truthiness: `bool("FALSE")` is `True`, and that mistake
# publishes all 31,893 historical contracts as current.
ACTIVE_VALUES = frozenset({"TRUE", "T", "1", "YES"})

# The feed's own null spellings, alongside the empty string.
NULL_VALUES = frozenset({"", "NA", "N/A", "NULL", "NAN"})

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
# `RG`) are absent from that set and they cover 952 of the 2,908 active rows.
#
# An unrecognised code therefore maps to `None` — one row resolving on name and
# team alone — rather than being passed through to 422 the batch it travels in.
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


@dataclass(frozen=True)
class ContractRow:
    """One active contract, in this collector's own row shape.

    `year_signed`, `years`, `total_value_usd` and `guaranteed_total_usd` are
    nullable because a blank is a *fact about the row* — "the upstream did not
    say" — and dropping the row over one absent term would discard the team,
    the value and the identity that are present. `capture.py` keeps that null
    distinct from the six that are null because the SOURCE never carries them.
    """

    otc_id: str
    player_name: str
    otc_position: str
    otc_team: str
    year_signed: int | None
    years: int | None
    total_value_usd: int | None
    guaranteed_total_usd: int | None


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
    in its path. It is still recorded, because a lake object a season later
    must say which of two feeds a row came from — and this one is going to be
    replaced.
    """
    return UPSTREAM_URL


def canonical_team(raw: str) -> str | None:
    """An OverTheCap club label as a canonical abbreviation, or `None`.

    `None` for a multi-club string and for anything unrecognised. See the
    module docstring: a wrong team is worse than an absent one at both ends —
    it scores as disagreement during resolution and it publishes a club the
    player does not play for.
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


def _is_active(raw: str) -> bool:
    return (raw or "").strip().upper() in ACTIVE_VALUES


def _int_or_none(raw: str) -> int | None:
    """A whole-dollar or whole-season integer, or `None` for the feed's nulls.

    Tolerant of a trailing `.0`, because a spreadsheet round-trip upstream
    would otherwise null every money column at once. Not tolerant of anything
    else: returning `None` for junk is what makes a column that changed shape
    visible as a null field rather than as a fabricated number.
    """
    text = (raw or "").strip()
    if text.upper() in NULL_VALUES:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        value = float(text)
    except ValueError:
        return None
    return int(value) if value.is_integer() else None


def _to_row(raw: dict[str, str]) -> ContractRow | None:
    """One CSV row as a `ContractRow`, or `None` if it cannot be one.

    Only a blank `player` disqualifies a row outright: the name is the join key
    and without it `player-identity` has nothing to resolve. Everything else
    degrades to a null field rather than costing the row.
    """
    name = (raw.get("player") or "").strip()
    if not name:
        return None
    years = _int_or_none(raw.get("years", ""))
    return ContractRow(
        otc_id=(raw.get("otc_id") or "").strip(),
        player_name=name,
        otc_position=(raw.get("position") or "").strip(),
        otc_team=(raw.get("team") or "").strip(),
        year_signed=_int_or_none(raw.get("year_signed", "")),
        # A zero- or negative-length contract is not a term, it is a bad cell.
        # Left null so `contract_end_season` is null rather than the season
        # BEFORE the deal was signed, which is a well-formed lie.
        years=years if years is not None and years >= 1 else None,
        total_value_usd=_int_or_none(raw.get("value", "")),
        guaranteed_total_usd=_int_or_none(raw.get("guaranteed", "")),
    )


def _sort_key(row: ContractRow) -> tuple:
    """Total order over the active rows sharing one `otc_id`; smallest wins.

    Newest signing first, then the longer deal, then the larger value, then the
    position code — the last purely to break a remaining tie the same way every
    time. Determinism is the whole requirement: the twenty-one real collisions
    differ only in `position` (`LT` versus `RT`), so without a total order the
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

    Keyed on `otc_id` and falling back to `(name, position)` when the feed
    omits one — never on the name alone. 38 distinct names appear twice in the
    active set (a QB and an edge rusher both called Josh Allen, among others),
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


async def fetch_active_contracts(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
) -> ContractsRead:
    """Every active contract in the feed, one row per player.

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

    async for raw in stream_csv_dicts(
        client,
        UPSTREAM_URL,
        required_columns=REQUIRED_COLUMNS,
        columns=COLUMNS,
        gzipped=True,
        # The same string the envelope records as `upstream.source_ref`, so the
        # cache key and the provenance cannot drift apart.
        etag_key=UPSTREAM_URL,
    ):
        # Checked BEFORE the row is built: 28,985 of 31,893 rows are historical,
        # and constructing a dataclass for each of them to throw it away is the
        # length half of the memory rule in `collector_core.streaming`.
        if not _is_active(raw.get("is_active", "")):
            continue
        row = _to_row(raw)
        if row is None:
            malformed += 1
            continue
        kept.append(row)

    rows, duplicate_active = _dedupe(kept)
    return ContractsRead(
        rows=rows, malformed=malformed, duplicate_active=duplicate_active
    )
