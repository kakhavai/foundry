import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from player_projections import main
from player_projections.client import fetch_projections
from player_projections.main import app

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "projections-snapshot"
SCHEMA_FILE = "snapshot.v1.schema.json"
FORMATS = ["standard", "half-ppr", "ppr"]


def test_schema_is_valid_json_schema():
    schema = json.loads((CONTRACTS / SCHEMA_FILE).read_text())
    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("fmt", FORMATS)
def test_schema_accepts_every_scoring_format(fmt):
    """Scoring changes the values in `rank`/`proj_points`, never the shape, so
    one schema covers all three documents."""
    schema = json.loads((CONTRACTS / SCHEMA_FILE).read_text())
    doc = json.loads((CONTRACTS / "fixtures" / "ppr-valid.json").read_text())
    doc["format"] = fmt

    Draft202012Validator(schema, format_checker=FormatChecker()).validate(doc)


def test_unknown_scoring_format_is_rejected():
    """`format` is an enum, not a free string — an invented scoring mode must
    not validate. This is what replaced the per-file `const` when the three
    schemas collapsed into one."""
    schema = json.loads((CONTRACTS / SCHEMA_FILE).read_text())
    doc = json.loads((CONTRACTS / "fixtures" / "ppr-valid.json").read_text())
    doc["format"] = "quarter-ppr"

    errors = list(Draft202012Validator(schema).iter_errors(doc))

    assert errors, "an unknown scoring format must fail validation"


def test_fixture_spreads_are_ordered():
    """JSON Schema cannot express this; assert it as a business rule instead."""
    doc = json.loads((CONTRACTS / "fixtures" / "ppr-valid.json").read_text())

    for player in doc["players"]:
        spread = player.get("proj_points")
        if spread is None:
            continue
        assert spread["floor"] <= spread["expected"] <= spread["ceiling"], (
            f"{player['id']} has a spread out of order: {spread}"
        )


def test_fixture_validates_against_ppr_schema():
    schema = json.loads((CONTRACTS / SCHEMA_FILE).read_text())
    fixture = json.loads((CONTRACTS / "fixtures" / "ppr-valid.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(fixture)


def test_bad_generated_at_is_rejected():
    """`format: date-time` is only enforced when a FormatChecker is attached."""
    schema = json.loads((CONTRACTS / SCHEMA_FILE).read_text())
    doc = json.loads((CONTRACTS / "fixtures" / "ppr-valid.json").read_text())
    doc["generated_at"] = "not-a-date-at-all"

    errors = list(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(doc)
    )

    assert errors, "a malformed generated_at must fail validation"


def test_dst_row_carrying_skill_player_fields_is_rejected():
    """A DST row must not also carry rank/proj_points — the conditional's
    `then` branch must positively exclude the `else` branch's shape."""
    schema = json.loads((CONTRACTS / SCHEMA_FILE).read_text())
    fixture = json.loads((CONTRACTS / "fixtures" / "ppr-valid.json").read_text())
    dst = next(p for p in fixture["players"] if p["pos"] == "DST")
    dst["rank"] = 5
    dst["proj_points"] = {"floor": 1, "expected": 2, "ceiling": 3}

    errors = list(Draft202012Validator(schema).iter_errors(fixture))

    assert errors, "a DST row carrying rank/proj_points must fail validation"


def test_wr_row_carrying_dst_fields_is_rejected():
    """A non-DST row must not also carry yahoo_rank/espn_rank — the
    conditional's `else` branch must positively exclude the `then` shape."""
    schema = json.loads((CONTRACTS / SCHEMA_FILE).read_text())
    fixture = json.loads((CONTRACTS / "fixtures" / "ppr-valid.json").read_text())
    wr = next(p for p in fixture["players"] if p["pos"] == "WR")
    wr["yahoo_rank"] = 1

    errors = list(Draft202012Validator(schema).iter_errors(fixture))

    assert errors, "a non-DST row carrying yahoo_rank must fail validation"


def test_only_one_snapshot_schema_exists():
    """The three per-format schemas differed only in `$id`, `title`, and
    `format.const` — identical shapes kept in sync by a test. One file makes
    that structural instead. A new `*.v1.schema.json` here means someone
    reintroduced the drift this consolidation removed."""
    schemas = sorted(p.name for p in CONTRACTS.glob("*.schema.json"))

    assert schemas == [SCHEMA_FILE]


@respx.mock
async def test_consumer_parses_a_schema_valid_snapshot():
    """The real contract assertion: a schema-valid document parses cleanly."""
    fixture = json.loads((CONTRACTS / "fixtures" / "ppr-valid.json").read_text())
    url = "https://example.test/ppr.json"
    respx.get(url).mock(return_value=httpx.Response(200, json=fixture))

    players = await fetch_projections(url)

    assert len(players) == 4
    assert {p["id"] for p in players} == {
        "p_8f3a21",
        "p_1c9e04",
        "p_4d7b12",
        "p_9a2f77",
    }
    assert players[0]["proj_points"]["expected"] == 12.4


OPENAPI = Path(__file__).resolve().parents[3] / "contracts" / "openapi"

REGENERATE_HINT = (
    "The service's OpenAPI surface changed.\n"
    "If the change is intentional, regenerate the snapshot:\n"
    "  cd services/player-projections && uv run python -c "
    '"import json,pathlib; from player_projections.main import app; '
    "pathlib.Path('../../contracts/openapi/player-projections.json').write_text("
    "json.dumps(app.openapi(), indent=2, sort_keys=True) + '\\n')\"\n"
    "and include it in the same PR so the surface change is explicit in review."
)


def test_openapi_snapshot_matches_committed_contract():
    committed = json.loads((OPENAPI / "player-projections.json").read_text())
    live = json.loads(json.dumps(app.openapi(), sort_keys=True))
    assert live == committed, REGENERATE_HINT


def test_documented_paths_are_present():
    paths = set(app.openapi()["paths"])
    assert paths == {"/health", "/metrics", "/projections"}


def response_shape(obj, prefix: str = "") -> list[str]:
    """Dotted key paths for a JSON body. Lists are represented by their first
    element with a `[]` marker, so `stadiums[].weather.temperature_c` pins a
    nested field name. Scalars terminate a path."""
    if isinstance(obj, dict):
        out: list[str] = []
        for key in sorted(obj):
            child = f"{prefix}.{key}" if prefix else key
            out.extend(response_shape(obj[key], child))
        return out or ([prefix] if prefix else [])
    if isinstance(obj, list):
        return response_shape(obj[0], f"{prefix}[]") if obj else [f"{prefix}[]"]
    return [prefix]


RESPONSES = (
    Path(__file__).resolve().parents[3]
    / "contracts"
    / "responses"
    / "player-projections.json"
)

SHAPE_HINT = (
    "A response body's field names changed.\n"
    "If intentional, regenerate contracts/responses/player-projections.json and "
    "include it in the same PR so the change is explicit in review."
)


async def test_response_shapes_match_committed_contract(monkeypatch):
    """Catches renamed or dropped response fields at any nesting depth.

    Data flows through the real production path, not a literal seeded straight
    into `_state`: `fetch_projections()` parses the committed snapshot
    fixture served over HTTP, `_poll_loop()` runs one real iteration and
    populates `_state`, and `/projections` serves it back through `TestClient`.
    A field rename in `client.py`'s return path or in the fixture itself now
    breaks this test — see the `main.py`/`client.py` rename check below.

    `response_shape` represents a list by its FIRST element only, so this
    contract pins skill-player fields (rank, proj_points.*, blurb) from the
    fixture's leading WR entry and does NOT cover DST's yahoo_rank/espn_rank.
    Those are asserted directly in
    tests/integration/test_app.py::test_populated_cache_is_served.
    """
    fixture = json.loads((CONTRACTS / "fixtures" / "ppr-valid.json").read_text())
    template = "https://example.test/{format}.json"
    monkeypatch.setenv("PROJECTIONS_SNAPSHOT_URL", template)

    async def stop_after_first(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", stop_after_first)

    committed = json.loads(RESPONSES.read_text())

    try:
        with respx.mock:
            # Each format's document declares its own `format`, so the poll
            # loop's expect_format check passes for all three.
            for fmt in main.FORMATS:
                respx.get(main._url_for(template, fmt)).mock(
                    return_value=httpx.Response(200, json={**fixture, "format": fmt})
                )
            with pytest.raises(asyncio.CancelledError):
                await main._poll_loop()

        with TestClient(app) as client:
            actual = {
                "/health": response_shape(client.get("/health").json()),
                "/projections": response_shape(client.get("/projections").json()),
                "/projections?pos=WR": response_shape(
                    client.get("/projections", params={"pos": "WR"}).json()
                ),
            }
    finally:
        for fmt in main.FORMATS:
            main._state[fmt] = main._empty_cache()

    assert actual == committed, SHAPE_HINT


# --- Outbound response schema -------------------------------------------------
#
# The OpenAPI snapshot cannot describe the 200 body (handlers return bare dicts,
# so FastAPI emits `"schema": {}`) and the response-shape contract pins field
# NAMES but not types. This schema is the type/constraint contract for what the
# frontend will consume. It is hand-written and design-first: the frontend does
# not exist yet.

API_CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "projections-api"
RESPONSE_SCHEMA_FILE = "response.v1.schema.json"


def _response_validator() -> Draft202012Validator:
    """Build a validator that can resolve the cross-file $ref to the player def.

    The response schema $refs `snapshot.v1.schema.json#/$defs/player` by its
    `$id` URI, which is not fetchable — foundry.internal does not resolve. The
    registry maps that URI to the file on disk so there is exactly one
    definition of a player row across the inbound and outbound contracts.
    """
    snapshot = json.loads((CONTRACTS / SCHEMA_FILE).read_text())
    response = json.loads((API_CONTRACTS / RESPONSE_SCHEMA_FILE).read_text())
    registry = Resource.from_contents(snapshot) @ Registry()
    return Draft202012Validator(
        response, format_checker=FormatChecker(), registry=registry
    )


def test_response_schema_is_valid_json_schema():
    schema = json.loads((API_CONTRACTS / RESPONSE_SCHEMA_FILE).read_text())
    Draft202012Validator.check_schema(schema)


def test_stub_mode_response_validates():
    """The empty response is a legal response, not a special case."""
    with TestClient(app) as client:
        body = client.get("/projections").json()

    _response_validator().validate(body)


async def test_populated_response_validates_through_the_real_path(monkeypatch):
    """The strongest form of this contract: data flows from the committed
    fixture through fetch_projections -> _poll_loop -> /projections, and the
    served body is validated against the outbound schema. Because player rows
    are $ref'd from the snapshot schema, this also proves the inbound and
    outbound contracts agree on a row.
    """
    fixture = json.loads((CONTRACTS / "fixtures" / "ppr-valid.json").read_text())
    template = "https://example.test/{format}.json"
    monkeypatch.setenv("PROJECTIONS_SNAPSHOT_URL", template)

    async def stop_after_first(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", stop_after_first)

    try:
        with respx.mock:
            for fmt in main.FORMATS:
                respx.get(main._url_for(template, fmt)).mock(
                    return_value=httpx.Response(200, json={**fixture, "format": fmt})
                )
            with pytest.raises(asyncio.CancelledError):
                await main._poll_loop()

        validator = _response_validator()
        with TestClient(app) as client:
            for fmt in main.FORMATS:
                body = client.get("/projections", params={"format": fmt}).json()
                assert body["count"] == 4
                validator.validate(body)

            filtered = client.get("/projections", params={"pos": "DST"}).json()
            validator.validate(filtered)
            assert filtered["count"] == 1
    finally:
        for fmt in main.FORMATS:
            main._state[fmt] = main._empty_cache()


def test_count_matches_projections_length():
    """JSON Schema cannot compare sibling properties, so the invariant the
    schema documents on `count` is asserted here instead."""
    main._state["ppr"]["projections"] = [
        {
            "id": f"p_{i}",
            "name": "x",
            "pos": "WR",
            "team": "SF",
            "rank": i + 1,
            "proj_points": {"floor": 1, "expected": 2, "ceiling": 3},
        }
        for i in range(7)
    ]
    try:
        with TestClient(app) as client:
            full = client.get("/projections").json()
            empty = client.get("/projections", params={"pos": "QB"}).json()
    finally:
        main._state["ppr"] = main._empty_cache()

    assert full["count"] == len(full["projections"]) == 7
    assert empty["count"] == len(empty["projections"]) == 0
