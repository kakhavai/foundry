from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from collector_core.envelope import ENVELOPE_VERSION, Coverage, Envelope, Upstream
from collector_core.lake import NullLakeWriter, S3LakeWriter, lake_key

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
