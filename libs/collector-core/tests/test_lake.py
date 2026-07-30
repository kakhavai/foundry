from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from collector_core.lake import (
    NullLakeWriter,
    S3LakeWriter,
    build_lake_writer_from_env,
    lake_key,
)

BUCKET = "foundry-signals-test"


def envelope(
    captured_at: datetime,
    week: int = 3,
    signals=None,
    signal_type: str = "venue_forecast_kickoff",
    envelope_version: str = ENVELOPE_VERSION,
) -> Envelope:
    return Envelope(
        envelope_version=envelope_version,
        collector="weather",
        signal_type=signal_type,
        captured_at=captured_at,
        upstream=Upstream("open-meteo", captured_at),
        scope={"season": 2026, "week": week},
        coverage=Coverage(expected=1, present=1, missing=[]),
        errors=[],
        signals=signals if signals is not None else [{"game_id": "2026_03_KC_BUF"}],
    )


def test_key_layout_partitions_by_season_and_week():
    key = lake_key(envelope(datetime(2026, 9, 17, 14, 3, tzinfo=UTC)))
    assert key == (
        "signals/weather/v1/season=2026/week=03/"
        "2026-09-17T14:03:00Z-venue_forecast_kickoff.json"
    )


def test_week_is_zero_padded_so_prefix_scans_sort():
    key = lake_key(envelope(datetime(2026, 9, 10, 9, 0, tzinfo=UTC), week=4))
    assert "week=04/" in key


@mock_aws
def test_write_puts_an_object_and_returns_its_key():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    writer = S3LakeWriter(BUCKET, client)

    key = writer.write(envelope(datetime(2026, 9, 17, 14, 3, tzinfo=UTC)))

    body = writer.read(key)
    assert body["collector"] == "weather"
    assert body["signals"][0]["game_id"] == "2026_03_KC_BUF"


@mock_aws
def test_two_captures_of_the_same_scope_are_two_objects():
    """Append-only: a later capture never overwrites an earlier one."""
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    writer = S3LakeWriter(BUCKET, client)

    writer.write(envelope(datetime(2026, 9, 15, 9, 0, tzinfo=UTC)))
    writer.write(envelope(datetime(2026, 9, 17, 9, 0, tzinfo=UTC)))

    keys = writer.list_keys("weather", "venue_forecast_kickoff", 2026, 3)
    assert len(keys) == 2


@mock_aws
def test_two_signal_types_from_one_pass_are_two_objects():
    """FINDING 1 regression: a capture pass writes `venue_forecast_kickoff`
    and `venue_conditions_current` with the *same* `captured_at` -- one
    frozen instant per pass, deliberately. Before `signal_type` was part of
    the key, both envelopes resolved to one key and the second `put_object`
    silently overwrote the first; the convergence route (and every consumer
    of `venue_forecast_kickoff`) would then find nothing at all.

    The old `envelope()` helper hardcoded `signal_type="venue_forecast_kickoff"`
    for every call in this file, so nothing here ever wrote two different
    signal types and this collision was invisible.
    """
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    writer = S3LakeWriter(BUCKET, client)

    same_instant = datetime(2026, 9, 17, 14, 3, tzinfo=UTC)
    forecast = envelope(same_instant, signal_type="venue_forecast_kickoff")
    current = envelope(same_instant, signal_type="venue_conditions_current")

    forecast_key = writer.write(forecast)
    current_key = writer.write(current)

    assert forecast_key != current_key

    all_objects = client.list_objects_v2(Bucket=BUCKET)["Contents"]
    assert len(all_objects) == 2

    assert writer.read(forecast_key)["signal_type"] == "venue_forecast_kickoff"
    assert writer.read(current_key)["signal_type"] == "venue_conditions_current"

    forecast_keys = writer.list_keys("weather", "venue_forecast_kickoff", 2026, 3)
    current_keys = writer.list_keys("weather", "venue_conditions_current", 2026, 3)
    assert forecast_keys == [forecast_key]
    assert current_keys == [current_key]


@mock_aws
def test_list_keys_honours_the_version_it_was_written_with():
    """M1 regression: `_partition_prefix` used to hardcode `v1` while
    `lake_key` used `envelope.envelope_version`. At envelope v2, writes would
    land under `/v2/` while a caller still scanning the hardcoded `/v1/`
    prefix found nothing at all.
    """
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    writer = S3LakeWriter(BUCKET, client)

    v2 = envelope(datetime(2026, 9, 17, 14, 3, tzinfo=UTC), envelope_version="2")
    key = writer.write(v2)

    assert "/v2/" in key
    assert writer.list_keys("weather", "venue_forecast_kickoff", 2026, 3, "2") == [key]
    assert writer.list_keys("weather", "venue_forecast_kickoff", 2026, 3, "1") == []


@mock_aws
def test_list_keys_returns_captured_at_order():
    """The convergence route depends on this ordering."""
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    writer = S3LakeWriter(BUCKET, client)

    for day in (17, 15, 16):
        writer.write(envelope(datetime(2026, 9, day, 9, 0, tzinfo=UTC)))

    keys = writer.list_keys("weather", "venue_forecast_kickoff", 2026, 3)
    assert keys == sorted(keys)
    assert "2026-09-15" in keys[0]
    assert "2026-09-17" in keys[-1]


def test_list_keys_sorts_pages_that_arrive_out_of_order():
    """Regression guard for the `sorted()` call itself.

    Real S3 (and moto) already returns list_objects_v2 pages in lexicographic
    order for this key layout, so a test built on moto can't distinguish
    `return sorted(keys)` from `return keys` — replacing the former with the
    latter still passes a moto-backed test. This fakes the paginator to hand
    back an out-of-order page so the sort is actually exercised.
    """
    prefix = "signals/weather/v1/season=2026/week=03/"
    suffix = "-venue_forecast_kickoff.json"
    unordered_keys = [
        prefix + "2026-09-17T09:00:00Z" + suffix,
        prefix + "2026-09-15T09:00:00Z" + suffix,
        prefix + "2026-09-16T09:00:00Z" + suffix,
    ]
    fake_paginator = MagicMock()
    fake_paginator.paginate.return_value = [
        {"Contents": [{"Key": key} for key in unordered_keys]}
    ]
    fake_client = MagicMock()
    fake_client.get_paginator.return_value = fake_paginator

    writer = S3LakeWriter(BUCKET, fake_client)
    keys = writer.list_keys("weather", "venue_forecast_kickoff", 2026, 3)

    assert keys == sorted(unordered_keys)
    assert keys[0].endswith("2026-09-15T09:00:00Z" + suffix)
    assert keys[-1].endswith("2026-09-17T09:00:00Z" + suffix)


@mock_aws
def test_list_keys_on_an_empty_partition_returns_empty():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    writer = S3LakeWriter(BUCKET, client)

    assert writer.list_keys("weather", "venue_forecast_kickoff", 2026, 9) == []


def test_null_writer_satisfies_the_interface_and_discards():
    writer = NullLakeWriter()
    key = writer.write(envelope(datetime(2026, 9, 17, 14, 3, tzinfo=UTC)))
    assert key == ""
    assert writer.list_keys("weather", "venue_forecast_kickoff", 2026, 3) == []


def test_null_writer_read_raises():
    with pytest.raises(KeyError):
        NullLakeWriter().read("anything")


def test_build_lake_writer_from_env_without_bucket_returns_null_writer(monkeypatch):
    monkeypatch.delenv("LAKE_BUCKET", raising=False)

    writer = build_lake_writer_from_env()

    assert isinstance(writer, NullLakeWriter)


def test_build_lake_writer_from_env_with_bucket_returns_bound_s3_writer(monkeypatch):
    """LAKE_BUCKET set, LAKE_ENDPOINT_URL unset — the production path, where
    the default endpoint and an instance role apply."""
    monkeypatch.setenv("LAKE_BUCKET", "foundry-signals")
    monkeypatch.delenv("LAKE_ENDPOINT_URL", raising=False)

    with patch("collector_core.lake.boto3.client") as mock_client:
        writer = build_lake_writer_from_env()

    assert isinstance(writer, S3LakeWriter)
    assert writer._bucket == "foundry-signals"
    mock_client.assert_called_once_with("s3", endpoint_url=None)


def test_build_lake_writer_from_env_passes_endpoint_url_through(monkeypatch):
    """LAKE_ENDPOINT_URL set — the Kind/MinIO development path."""
    monkeypatch.setenv("LAKE_BUCKET", "foundry-signals")
    monkeypatch.setenv("LAKE_ENDPOINT_URL", "http://minio.local:9000")

    with patch("collector_core.lake.boto3.client") as mock_client:
        build_lake_writer_from_env()

    mock_client.assert_called_once_with("s3", endpoint_url="http://minio.local:9000")
