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


def envelope(captured_at: datetime, week: int = 3, signals=None) -> Envelope:
    return Envelope(
        envelope_version=ENVELOPE_VERSION,
        collector="weather",
        signal_type="venue_forecast_kickoff",
        captured_at=captured_at,
        upstream=Upstream("open-meteo", captured_at),
        scope={"season": 2026, "week": week},
        coverage=Coverage(expected=1, present=1, missing=[]),
        errors=[],
        signals=signals if signals is not None else [{"game_id": "2026_03_KC_BUF"}],
    )


def test_key_layout_partitions_by_season_and_week():
    key = lake_key(envelope(datetime(2026, 9, 17, 14, 3, tzinfo=UTC)))
    assert key == ("signals/weather/v1/season=2026/week=03/2026-09-17T14:03:00Z.json")


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
    unordered_keys = [
        prefix + "2026-09-17T09:00:00Z.json",
        prefix + "2026-09-15T09:00:00Z.json",
        prefix + "2026-09-16T09:00:00Z.json",
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
    assert keys[0].endswith("2026-09-15T09:00:00Z.json")
    assert keys[-1].endswith("2026-09-17T09:00:00Z.json")


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
