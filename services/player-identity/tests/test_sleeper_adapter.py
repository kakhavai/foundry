"""The two levels of upstream schema validation, and the ETag.

Both levels exist because both failures are silent. A document-level rename
drops every Tier-1 link while every record still parses; a per-record rename
writes `jersey_number: null` for all ~2,900 players and looks like data.
"""

import httpx
import pytest
import respx
from conftest import player, sleeper_document

from player_identity.adapters.sleeper import (
    PLAYERS_URL,
    UpstreamSchemaError,
    fetch_players,
    record_schema_errors,
    validate_document,
)
from player_identity.identity import CROSSWALK_KEYS


@respx.mock
async def test_fetch_players_returns_the_etag_as_source_ref():
    respx.get(PLAYERS_URL).mock(
        return_value=httpx.Response(
            200, json=sleeper_document(player("1")), headers={"ETag": 'W/"abc123"'}
        )
    )
    async with httpx.AsyncClient() as client:
        payload, source_ref = await fetch_players(client)

    assert source_ref == 'W/"abc123"'
    assert set(payload) == {"1"}


@respx.mock
async def test_a_missing_etag_is_none_not_a_fabricated_stand_in():
    respx.get(PLAYERS_URL).mock(
        return_value=httpx.Response(200, json=sleeper_document(player("1")))
    )
    async with httpx.AsyncClient() as client:
        _, source_ref = await fetch_players(client)

    assert source_ref is None


@respx.mock
async def test_an_http_error_propagates():
    respx.get(PLAYERS_URL).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_players(client)


def test_document_validation_accepts_a_healthy_document():
    validate_document(sleeper_document(player("1"), player("2")), CROSSWALK_KEYS)


@pytest.mark.parametrize("payload", [[], {}, "nope", None, 5])
def test_document_validation_rejects_a_non_object_or_empty_payload(payload):
    with pytest.raises(UpstreamSchemaError):
        validate_document(payload, CROSSWALK_KEYS)


def test_document_validation_rejects_a_payload_with_no_record_objects():
    with pytest.raises(UpstreamSchemaError, match="no record objects"):
        validate_document({"1": "not a record"}, CROSSWALK_KEYS)


def test_a_renamed_crosswalk_key_fails_the_document_loudly():
    """The whole point of the document level. Rename `gsis_id` upstream and
    every record still parses, every field still maps, and every Tier-1 link
    silently disappears."""
    document = sleeper_document(player("1"), player("2"))
    for record in document.values():
        record["nfl_id"] = record.pop("gsis_id")

    with pytest.raises(UpstreamSchemaError, match="gsis_id"):
        validate_document(document, CROSSWALK_KEYS)


def test_one_record_carrying_a_source_is_enough():
    """Most players genuinely lack most crosswalk ids. The assertion is that
    the *key* still exists somewhere, not that every player has one."""
    document = sleeper_document(player("1"), player("2", crosswalk=False))
    validate_document(document, CROSSWALK_KEYS)


def test_record_schema_errors_names_the_missing_keys():
    record = player("1")
    del record["number"]
    del record["status"]
    assert record_schema_errors(record) == ["number", "status"]


def test_record_schema_errors_is_empty_for_a_null_but_present_key():
    """Present-though-nullable: a genuinely unknown jersey number is data, a
    missing `number` key is a schema move."""
    assert record_schema_errors(player("1", number=None)) == []


def test_record_schema_errors_rejects_a_non_object():
    assert record_schema_errors("nope") == ["<record is not an object>"]
