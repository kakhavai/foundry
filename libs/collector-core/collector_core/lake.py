"""Append-only signal lake.

Objects are never mutated or deleted in place. A correction lands as a new
object with a later `captured_at`, and the generator resolves by recency —
nothing is lost, and the revision itself is visible.

Partitioning by season and week means a training window is one prefix scan
rather than a full-bucket listing.
"""

import json
import os
from typing import Protocol

import boto3

from .envelope import Envelope


def lake_key(envelope: Envelope) -> str:
    """signals/<collector>/v<version>/season=<YYYY>/week=<NN>/<captured_at>.json

    Week is zero-padded so lexicographic prefix listing is also chronological
    ordering — week=10 must not sort before week=2.
    """
    captured_at = envelope.to_dict()["captured_at"]
    season = envelope.scope["season"]
    week = int(envelope.scope["week"])
    return (
        f"signals/{envelope.collector}/v{envelope.envelope_version}"
        f"/season={season}/week={week:02d}/{captured_at}.json"
    )


def _partition_prefix(collector: str, season: int, week: int) -> str:
    return f"signals/{collector}/v1/season={season}/week={week:02d}/"


class LakeWriter(Protocol):
    def write(self, envelope: Envelope) -> str: ...

    def list_keys(
        self, collector: str, signal_type: str, season: int, week: int
    ) -> list[str]: ...

    def read(self, key: str) -> dict: ...


class S3LakeWriter:
    """S3-API backend. Real S3 on EKS, MinIO on Kind — same code path."""

    def __init__(self, bucket: str, client) -> None:
        self._bucket = bucket
        self._client = client

    def write(self, envelope: Envelope) -> str:
        key = lake_key(envelope)
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=json.dumps(envelope.to_dict(), sort_keys=True).encode(),
            ContentType="application/json",
        )
        return key

    def list_keys(
        self, collector: str, signal_type: str, season: int, week: int
    ) -> list[str]:
        """Keys in the partition, in captured_at order.

        `signal_type` is not part of the key layout — one capture writes one
        envelope per signal type into the same partition — so results are
        filtered by reading, not by prefix. Sorted because the convergence
        route depends on the ordering.
        """
        paginator = self._client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(
            Bucket=self._bucket, Prefix=_partition_prefix(collector, season, week)
        ):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return sorted(keys)

    def read(self, key: str) -> dict:
        body = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        return json.loads(body)


class NullLakeWriter:
    """Discards writes. Used in tests and whenever LAKE_BUCKET is unset.

    Deliberately not a silent no-op in production: build_lake_writer_from_env
    logs at construction, so a collector running without a lake is visible
    rather than assumed.
    """

    def write(self, envelope: Envelope) -> str:
        return ""

    def list_keys(
        self, collector: str, signal_type: str, season: int, week: int
    ) -> list[str]:
        return []

    def read(self, key: str) -> dict:
        raise KeyError(f"NullLakeWriter holds no objects (requested {key!r})")


def build_lake_writer_from_env() -> LakeWriter:
    """Construct from LAKE_BUCKET / LAKE_ENDPOINT_URL.

    LAKE_ENDPOINT_URL points at MinIO on Kind and is unset on EKS, where the
    default endpoint and an IRSA-provided role apply. The code path is identical.
    """
    bucket = os.getenv("LAKE_BUCKET", "")
    if not bucket:
        return NullLakeWriter()
    endpoint = os.getenv("LAKE_ENDPOINT_URL") or None
    client = boto3.client("s3", endpoint_url=endpoint)
    return S3LakeWriter(bucket, client)
