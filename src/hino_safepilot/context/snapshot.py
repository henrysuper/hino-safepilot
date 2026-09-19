"""Versioned context snapshots with a hashed, reproducible manifest.

A snapshot bundles the raw provider payloads used for one offline experiment so
that a later replay reads exactly the same bytes. The manifest records
provenance (provider, dataset, sha256), coverage (time window and a coarse
bounding box), and reproducibility metadata. It deliberately records only
coarse (0.1-degree) geography so precise HINO traces never enter an artifact
that could be shared, and it never stores credentials or request headers.

Everything here is provisional: the HINO timezone is unresolved, so the manifest
stamps ``hino_timestamp_timezone = "UNRESOLVED"`` and observation times are
treated as parsed-but-not-authoritative until the organizer confirms semantics.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .cache import ContextCache
from .joining import ContextObservation

MANIFEST_SCHEMA_VERSION = "context-snapshot-manifest-v1"
UNRESOLVED_TIMEZONE = "UNRESOLVED"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


@dataclass(frozen=True)
class SnapshotObservationStats:
    """Coarse, shareable summary of one provider's parsed observations."""

    observation_count: int
    source_time_start: datetime | None
    source_time_end: datetime | None
    available_time_end: datetime | None
    coarse_latitude_min: float | None
    coarse_latitude_max: float | None
    coarse_longitude_min: float | None
    coarse_longitude_max: float | None
    coarse_cell_count: int
    contains_mock: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "observation_count": self.observation_count,
            "source_time_start": _iso(self.source_time_start),
            "source_time_end": _iso(self.source_time_end),
            "available_time_end": _iso(self.available_time_end),
            "coarse_bounding_box_0_1_degree": {
                "latitude_min": self.coarse_latitude_min,
                "latitude_max": self.coarse_latitude_max,
                "longitude_min": self.coarse_longitude_min,
                "longitude_max": self.coarse_longitude_max,
            },
            "coarse_0_1_degree_cell_count": self.coarse_cell_count,
            "contains_mock": self.contains_mock,
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def summarize_observations(observations: Iterable[ContextObservation]) -> SnapshotObservationStats:
    """Reduce parsed observations to a privacy-preserving coarse summary."""

    rows = list(observations)
    if not rows:
        return SnapshotObservationStats(
            observation_count=0,
            source_time_start=None,
            source_time_end=None,
            available_time_end=None,
            coarse_latitude_min=None,
            coarse_latitude_max=None,
            coarse_longitude_min=None,
            coarse_longitude_max=None,
            coarse_cell_count=0,
            contains_mock=False,
        )
    source_times = [row.source_timestamp for row in rows]
    available_times = [row.available_timestamp for row in rows]
    coarse_lat = [round(row.latitude_deg, 1) for row in rows]
    coarse_lon = [round(row.longitude_deg, 1) for row in rows]
    cells = {(la, lo) for la, lo in zip(coarse_lat, coarse_lon)}
    return SnapshotObservationStats(
        observation_count=len(rows),
        source_time_start=min(source_times),
        source_time_end=max(source_times),
        available_time_end=max(available_times),
        coarse_latitude_min=min(coarse_lat),
        coarse_latitude_max=max(coarse_lat),
        coarse_longitude_min=min(coarse_lon),
        coarse_longitude_max=max(coarse_lon),
        coarse_cell_count=len(cells),
        contains_mock=any(row.is_mock for row in rows),
    )


@dataclass
class SnapshotBuilder:
    """Accumulates raw payloads for one snapshot, then writes cache + manifest.

    ``snapshot_id`` names the versioned dataset used by an experiment. Raw bytes
    are stored through the content-addressed cache (filenames carry no HINO
    identifiers). The manifest lists each payload's provider, dataset, request
    identity, sha256, and an optional coarse observation summary.
    """

    snapshot_id: str
    cache: ContextCache
    _entries: list[dict[str, Any]] = field(default_factory=list)
    _payload_hashes: list[str] = field(default_factory=list)

    def add_payload(
        self,
        *,
        provider: str,
        dataset: str,
        request_identity: str,
        payload: Any,
        is_mock: bool,
        observations: Iterable[ContextObservation] | None = None,
    ) -> str:
        """Persist one raw payload and record its provenance. Returns the sha256."""

        canonical = _canonical_json_bytes(payload)
        digest = _sha256_bytes(canonical)
        self.cache.put_json(provider, dataset, request_identity, payload)
        stats = summarize_observations(observations) if observations is not None else None
        self._entries.append(
            {
                "provider": provider,
                "dataset": dataset,
                "request_identity": request_identity,
                "payload_sha256": digest,
                "is_mock": bool(is_mock),
                "observation_summary": stats.as_dict() if stats is not None else None,
            }
        )
        self._payload_hashes.append(digest)
        return digest

    def build_manifest(self, *, extra_notes: dict[str, Any] | None = None) -> dict[str, Any]:
        """Assemble the manifest, including a stable snapshot content hash."""

        ordered = sorted(self._entries, key=lambda entry: (entry["provider"], entry["dataset"], entry["request_identity"]))
        providers = sorted({entry["provider"] for entry in ordered})
        contains_mock = any(entry["is_mock"] for entry in ordered)
        content_hash = _sha256_bytes(_canonical_json_bytes(ordered))
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "snapshot_id": self.snapshot_id,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "hino_timestamp_timezone": UNRESOLVED_TIMEZONE,
            "empirical_claim_status": "PROVISIONAL_NO_EMPIRICAL_CLAIM",
            "providers": providers,
            "contains_mock": contains_mock,
            "payload_count": len(ordered),
            "snapshot_content_sha256": content_hash,
            "payloads": ordered,
            "notes": extra_notes or {},
        }
        return manifest

    def write_manifest(self, path: Path, *, extra_notes: dict[str, Any] | None = None) -> dict[str, Any]:
        manifest = self.build_manifest(extra_notes=extra_notes)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return manifest


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "UNRESOLVED_TIMEZONE",
    "SnapshotBuilder",
    "SnapshotObservationStats",
    "summarize_observations",
]
