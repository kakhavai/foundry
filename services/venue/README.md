# venue

The fixed properties of the place a game is played — the counterpart to
everything [`weather`](../weather/) reports about the same building changing
hour to hour. Surface and altitude move rushing efficiency, injury risk and
kicking distance in ways that persist all season and are invisible in a
player's own game log.

| | |
|---|---|
| Port | `8018` |
| Gateway path | `/collectors/venue` |
| Cadence class | `static reference` (re-read daily, appended on change) |
| Signal types | `venue_static`, `venue_game_assignment` |
| Depends on | nothing |
| Scope-aware | no — venues are not players |

## What it captures

**`venue_static`** — one revision of one venue's fixed properties, from
[`venue/reference.py`](venue/reference.py): a **committed reference table**,
the primary source the phase-8 spec names. It reaches no third party at all,
which is what makes this signal type fully deterministic and lets the test suite
assert real captured output rather than a mock's.

**`venue_game_assignment`** — which building each game is actually played in.
This one cannot be static: "the home team's stadium" is wrong roughly a dozen
times a year (London, Munich, São Paulo, neutral-site relocations, and both
MetLife tenants), and the game list moves with flex scheduling and
postponements. It reads the nflverse game table, the same feed
`schedule-context` reads, streamed and filtered as it is parsed.

## Revisions are append-only. That is the whole design.

A surface replacement or roof retrofit produces a **new** revision whose
`effective_from` is the install date, and **closes** the prior one by setting
its `effective_to`. Nothing is ever edited in place — `effective_to` is not even
a stored field, it is derived from the next record's `effective_from`, so there
is nothing for an adapter to overwrite.

The failure that defends against is silent. Because the data is nominally
static, an adapter that mutates a record retroactively applies a mid-season
change to the whole season: a Week 2 game gets attributed a surface that was not
installed until Week 11. Nothing errors, coverage stays at 1.0, and the season
simply becomes internally consistent with a fiction.

Two assertions catch it, and both are real tests in
[`tests/test_revisions.py`](tests/test_revisions.py):

1. **No game may resolve to a revision whose `[effective_from, effective_to)`
   window excludes its kickoff date.** Enforced at write time — a violation is
   a coverage miss with reason `revision_window_excludes_kickoff` and a
   non-zero `venue_revision_window_misses` — and re-checkable at read time,
   because every assignment row carries the window it was resolved against.
2. **A per-season count of venues with exactly one revision**, published as
   `venue_single_revision_venues`. A venue known to have changed surfaces
   showing a single revision is the tell.

## Which fields are populated, and which are null

**The committed table does not guess.** Where a field could not be sourced to
the standard the rest of it is held to, it is `null` — not a plausible-looking
value. [`venue/reference.py`](venue/reference.py)'s module docstring is the
authoritative list and gives a reason per field;
`tests/test_reference_table.py` asserts the claim so the docstring cannot
quietly drift.

| | Fields |
|---|---|
| Populated everywhere | `venue_id`, `name`, `city`, `country`, `latitude`, `longitude`, `timezone`, `roof_type`, `home_team_ids` (derived), `content_hash` |
| Populated selectively | `altitude_ft` (Denver and Estadio Azteca only), `surface_class` (null on `gillette` and `highmark`, and on every neutral-site venue) |
| Null everywhere, deliberately | `surface_product`, `surface_installed_on`, `surface_last_resurfaced_on`, `roof_state_policy`, `field_orientation_deg`, `seating_capacity`, `crowd_noise_profile`, `year_built`, `year_last_renovated` |

The consequence worth stating outright: because no surface change has a sourced
install date, **every venue in the table today has exactly one revision**, and
`venue_single_revision_venues` reports that on every pass. That is the honest
state of the data, published as a number rather than buried in a footnote.

`TABLE_COMPILED_ON` is every record's `effective_from`. It is not a construction
date: the table asserts its contents from that date forward and makes no claim
before it, so a kickoff earlier than it resolves to **no** revision rather than
to a back-dated guess.

## Routes

The standard five from `collector_core.routes`: `GET /health`, `GET /metrics`,
`GET /catalog`, `GET /signals`, `POST /refresh`. Everything except `/health` and
`/metrics` requires `Authorization: Bearer <token>`.

Plus one:

**`GET /venues/{venue_id}/revisions`** — a venue's full ordered revision
history, so a consumer can resolve the record true on a given date without
scanning the lake. `?on=YYYY-MM-DD` resolves that date directly. It returns
**zero** revisions for a date the table makes no claim about, never the closest
one, and **404** for an unknown venue id — `[]` would be filed as "that venue
has no history" rather than as the caller's typo.

Served from the committed table rather than from the lake, so it answers before
any capture has run and cannot be stale.

`POST /refresh` returns **202 — accepted, not done**. The capture runs as a
background task; poll `/signals` rather than reading it on the next line.

## Cadence: appended on change, not on schedule

`static reference` re-reads every 24 hours. Publishing a byte-identical envelope
each time would fill an append-only lake with 365 objects a year saying the same
thing, so a pass whose content digest matches the last one **this process**
published raises `UpstreamUnchanged` — `/catalog` reports a fresh pass while
`/signals` keeps serving the same rows.

Per process rather than per lake, deliberately: reading the last digest back out
of the lake would make a pod restart find a match, skip the publish against an
empty `CaptureState`, and serve nothing from `/signals` until the table next
changed — months, for a static reference. The in-memory version costs exactly
one redundant snapshot per restart, the same trade `ETagStore` makes.

## Deployed with `CAPTURE_ENABLED=false`

`venue_static` needs no network, but `venue_game_assignment` streams a ~2.1 MB
feed, and a `static reference` answer changes a handful of times a season. The
Kind cluster is rebuilt on every CI run and every pod restart re-captures, so
the loop stays off there. A dispatched `POST /refresh` reaches the upstream
regardless of the flag — which is why `smoke.sh` does not post one.

## Tests

```bash
cd services/venue
uv run pytest -v
```

## Known follow-ups

- **`schedule-context` still carries its own venue table.**
  [`schedule_context/venues.py`](../schedule-context/schedule_context/venues.py)
  calls itself transitional and names this collector as its replacement. It is
  deliberately **not** rewired here — that is a change with its own risk — so
  two tables now describe the same 30 buildings. Everything load-bearing in it
  was carried over (see this collector's `reference.py` docstring) and pinned by
  `tests/test_reference_table.py`, but they can still drift until the rewiring
  lands.
- **`weather.crosswind_component_mph` stays null**, because
  `field_orientation_deg` is not sourced here.
- **`surface_product` and dated surface changes** are the first thing to add.
  The revision machinery is built and tested for exactly that; only the source
  is missing.
