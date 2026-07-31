"""The upstream adapter — the only module that knows the wire format.

Kept separate from `capture.py` so the orchestration (coverage accounting,
envelopes, the lake) can be tested against a fake upstream without mocking
HTTP, and so swapping the upstream touches one file.

**Two memory rules, both learned the hard way.** roster-scope's first deploy was
OOMKilled at a 256Mi limit because a ~37 MB upstream document was buffered
three times over:

1. **Never hold an upstream response more than once.** `response.text` after
   `response.content`, or `json.loads(response.text)`, is two copies.
2. **Filter as you parse.** Build the rows you are keeping; never materialise
   the whole document and then narrow it. For a large CSV upstream use
   `collector_core.streaming.stream_csv_dicts`, which does both.
"""

from datetime import datetime

import httpx

# Names the upstream in every envelope's `upstream.adapter`. Change it when the
# upstream changes: it is how a consumer tells two sources of the same signal
# apart in the lake.
UPSTREAM_ADAPTER = "nflverse-depth-charts"

# TODO: the real upstream. While this is empty the placeholder branch in
# `fetch_rows` runs, so a freshly scaffolded collector deploys, captures and
# serves before its upstream exists — the same stub-mode reasoning
# player-projections uses for `PROJECTIONS_SNAPSHOT_URL`.
UPSTREAM_URL = ""

# DELETE ME along with the branch below. Deterministic, offline, and shaped
# exactly like `build_signal` expects, so the generated tests are real tests of
# the orchestration rather than of a mock.
PLACEHOLDER_KEYS: tuple[str, ...] = (
    "placeholder-a",
    "placeholder-b",
    "placeholder-c",
)


def source_ref(season: int, week: int) -> str | None:
    """The exact upstream artifact this pass read, recorded in the envelope.

    Not decorative: it is what makes a lake object reproducible, and what tells
    a reader which of two feeds a row came from a season later.
    """
    if not UPSTREAM_URL:
        return None
    return UPSTREAM_URL.format(season=season, week=week)


async def fetch_rows(
    season: int,
    week: int,
    *,
    client: httpx.AsyncClient,
    now: datetime,
) -> list[dict]:
    """One upstream fetch, parsed into this collector's own row shape.

    Raises on an upstream failure rather than returning an empty list. That is
    deliberate: `capture` turns the exception into a `present: 0` envelope with
    a populated `errors` array, and an empty list would instead be recorded as
    a successful capture of nothing.
    """
    if not UPSTREAM_URL:
        return [
            {"key": key, "value": float(index)}
            for index, key in enumerate(PLACEHOLDER_KEYS)
        ]

    response = await client.get(source_ref(season, week))
    response.raise_for_status()
    # Parse straight off the response — see the module docstring. Do not assign
    # `response.text` and then parse that.
    return [{"key": row["key"], "value": row["value"]} for row in response.json()]
