"""Fixture-only tests for the external-context pipeline (download/snapshot/quality).

No real network call is made: downloaders receive an in-memory fake transport.
No credentials are required unless a download method is explicitly invoked, and
the guardrail tests assert the missing-credential failure happens before any
transport call. Everything stays provisional (HINO timezone unresolved).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from hino_safepilot.context import (
    ContextDownloadError,
    CWAHistoricalDownloader,
    HttpResponse,
    MissingCredentialError,
    SnapshotBuilder,
    TDXHistoricalDownloader,
    build_downloader,
    build_quality_report,
    check_observation_integrity,
    check_temporal_leakage,
    parse_cwa_rainfall_observations,
    parse_tdx_vd_live,
)
from hino_safepilot.context.cache import ContextCache
from hino_safepilot.context.joining import ContextObservation


UTC = timezone.utc


def _tdx_payload() -> dict:
    return {
        "VDLives": [
            {
                "VDID": "VD-1",
                "DataCollectTime": "2025-09-20T08:00:00+08:00",
                "UpdateTime": "2025-09-20T08:00:30+08:00",
                "LinkFlows": [
                    {"Lanes": [{"Speed": 40, "Vehicles": [{"Volume": 12}]}]}
                ],
            }
        ]
    }


def _cwa_payload() -> dict:
    return {
        "records": {
            "Station": [
                {
                    "StationId": "S1",
                    "ObsTime": {"DateTime": "2025-09-20T08:00:00+08:00"},
                    "GeoInfo": {
                        "Coordinates": [
                            {"StationLatitude": 25.0, "StationLongitude": 121.5}
                        ]
                    },
                    "RainfallElement": {
                        "Past10Min": {"Precipitation": 1.0},
                        "Past1hr": {"Precipitation": 5.0},
                        "Past3hr": {"Precipitation": 9.0},
                    },
                }
            ]
        }
    }


class _FakeTransport:
    """Records calls and returns a canned response; never touches the network."""

    def __init__(self, response: HttpResponse):
        self.response = response
        self.calls: list = []

    def __call__(self, request, *, timeout_seconds):
        self.calls.append((request, timeout_seconds))
        return self.response


# --- credential guardrails ------------------------------------------------


def test_tdx_download_requires_credentials_before_any_transport_call(monkeypatch):
    monkeypatch.delenv("TDX_CLIENT_ID", raising=False)
    monkeypatch.delenv("TDX_CLIENT_SECRET", raising=False)
    transport = _FakeTransport(HttpResponse(200, b"{}"))
    downloader = TDXHistoricalDownloader(transport=transport)
    with pytest.raises(MissingCredentialError):
        downloader.download_json("https://tdx.transportdata.tw/api/basic/v2/Road/Traffic")
    assert transport.calls == []  # no network attempt without credentials


def test_cwa_url_builder_requires_key(monkeypatch):
    monkeypatch.delenv("CWA_API_KEY", raising=False)
    downloader = CWAHistoricalDownloader(transport=_FakeTransport(HttpResponse(200, b"{}")))
    with pytest.raises(MissingCredentialError):
        downloader.build_url("O-A0002-001")


def test_build_downloader_rejects_ncdr():
    with pytest.raises(ContextDownloadError):
        build_downloader("ncdr")


def test_cwa_download_with_key_uses_fake_transport(monkeypatch):
    monkeypatch.setenv("CWA_API_KEY", "fake-key")
    transport = _FakeTransport(HttpResponse(200, json.dumps(_cwa_payload()).encode("utf-8")))
    downloader = CWAHistoricalDownloader(transport=transport)
    payload = downloader.download_json("O-A0002-001")
    assert transport.calls, "transport should be invoked once credentials exist"
    assert payload["records"]["Station"][0]["StationId"] == "S1"
    # The Authorization key rides in the URL query, not in a header artifact.
    request = transport.calls[0][0]
    assert "Authorization=fake-key" in request.url
    assert "Authorization" not in request.headers


def test_transport_error_is_normalized(monkeypatch):
    monkeypatch.setenv("CWA_API_KEY", "fake-key")

    def broken(_request, *, timeout_seconds):
        raise RuntimeError("boom")

    downloader = CWAHistoricalDownloader(transport=broken)
    with pytest.raises(ContextDownloadError):
        downloader.download_json("O-A0002-001")


# --- snapshot + manifest --------------------------------------------------


def test_snapshot_builder_writes_cache_and_hashed_manifest(tmp_path):
    cache = ContextCache(tmp_path / "cache")
    builder = SnapshotBuilder(snapshot_id="snap-20250920", cache=cache)
    vd_locations = {"VD-1": (25.0, 121.5, "urban")}
    tdx_obs = parse_tdx_vd_live(_tdx_payload(), vd_locations=vd_locations, is_mock=False)
    cwa_obs = parse_cwa_rainfall_observations(_cwa_payload(), is_mock=False)
    builder.add_payload(
        provider="tdx",
        dataset="Traffic-v2-VDLive",
        request_identity="2025-09-20T08:00",
        payload=_tdx_payload(),
        is_mock=False,
        observations=tdx_obs,
    )
    builder.add_payload(
        provider="cwa",
        dataset="O-A0002-001",
        request_identity="2025-09-20T08:00",
        payload=_cwa_payload(),
        is_mock=False,
        observations=cwa_obs,
    )
    manifest = builder.write_manifest(tmp_path / "manifest.json")
    assert manifest["snapshot_id"] == "snap-20250920"
    assert manifest["hino_timestamp_timezone"] == "UNRESOLVED"
    assert manifest["empirical_claim_status"] == "PROVISIONAL_NO_EMPIRICAL_CLAIM"
    assert manifest["payload_count"] == 2
    assert sorted(manifest["providers"]) == ["cwa", "tdx"]
    assert not manifest["contains_mock"]
    assert len(manifest["snapshot_content_sha256"]) == 64
    # coarse geo only: bounding box present, no raw trace exported
    tdx_entry = next(p for p in manifest["payloads"] if p["provider"] == "tdx")
    box = tdx_entry["observation_summary"]["coarse_bounding_box_0_1_degree"]
    assert box["latitude_min"] == pytest.approx(25.0)
    # cached raw payload round-trips
    assert cache.get_json("tdx", "Traffic-v2-VDLive", "2025-09-20T08:00") == _tdx_payload()


def test_snapshot_manifest_hash_is_order_independent(tmp_path):
    def build(order):
        cache = ContextCache(tmp_path / f"cache-{order[0]}")
        b = SnapshotBuilder(snapshot_id="s", cache=cache)
        for provider in order:
            b.add_payload(
                provider=provider,
                dataset="d",
                request_identity="r",
                payload={"provider": provider},
                is_mock=False,
            )
        return b.build_manifest()["snapshot_content_sha256"]

    assert build(["tdx", "cwa"]) == build(["cwa", "tdx"])


def test_mock_payload_flags_manifest(tmp_path):
    cache = ContextCache(tmp_path / "cache")
    builder = SnapshotBuilder(snapshot_id="s", cache=cache)
    builder.add_payload(
        provider="tdx", dataset="d", request_identity="r", payload={"x": 1}, is_mock=True
    )
    assert builder.build_manifest()["contains_mock"] is True


# --- quality / leakage / spatial join -------------------------------------


def _obs(source: datetime, available: datetime, lat=25.0, lon=121.5, ref="S1") -> ContextObservation:
    return ContextObservation(
        source_timestamp=source,
        available_timestamp=available,
        latitude_deg=lat,
        longitude_deg=lon,
        values={"rainfall_1h_mm": 5.0},
        source_reference=ref,
    )


def test_integrity_flags_available_before_source():
    base = datetime(2025, 9, 20, 0, 0, tzinfo=UTC)
    good = _obs(base, base + timedelta(seconds=30))
    bad = _obs(base, base - timedelta(seconds=30))
    report = check_observation_integrity([good, bad]).as_dict()
    assert report["available_before_source_count"] == 1
    assert report["passed"] is False


def test_temporal_leakage_skipped_without_decision_times():
    result = check_temporal_leakage([], decision_times=None)
    assert result["leakage_check_performed"] is False
    assert result["skip_reason"].startswith("NO_ADMISSIBLE_DECISION_TIMES")


def test_temporal_leakage_requires_aware_decision_times():
    base = datetime(2025, 9, 20, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        check_temporal_leakage([_obs(base, base)], decision_times=[datetime(2025, 9, 20, 0, 5)])


def test_temporal_leakage_counts_future_available():
    base = datetime(2025, 9, 20, 0, 0, tzinfo=UTC)
    decision = base + timedelta(seconds=60)
    future_available = _obs(base, base + timedelta(seconds=120))  # published after decision
    result = check_temporal_leakage([future_available], decision_times=[decision])
    assert result["leakage_check_performed"] is True
    assert result["future_available_observations_seen"] == 1
    assert result["passed"] is True  # excluded by construction, not joined


def test_build_quality_report_spatial_join():
    base = datetime(2025, 9, 20, 0, 0, tzinfo=UTC)
    observations = [_obs(base, base)]
    decision = base + timedelta(seconds=120)
    report = build_quality_report(
        observations,
        decision_times=[decision],
        spatial_queries=[(decision, 25.01, 121.51)],
        maximum_age_seconds=1800,
        maximum_distance_m=30000,
    )
    assert report["hino_timestamp_timezone"] == "UNRESOLVED"
    assert report["spatial_join"]["matched_count"] == 1
    assert report["spatial_join"]["match_rate"] == 1.0
    assert report["observation_integrity"]["passed"] is True


# --- CLI: snapshot (fixtures) -> quality ----------------------------------


def _write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_cli_snapshot_from_fixtures_then_quality(tmp_path, capsys):
    from hino_safepilot.context.cli import main

    # Local fixture payloads (no network, no credentials).
    tdx_fixture = _write(tmp_path / "tdx.json", _tdx_payload())
    cwa_fixture = _write(tmp_path / "cwa.json", _cwa_payload())
    plan = {
        "snapshot_id": "cli-snap",
        "entries": [
            {
                "provider": "tdx",
                "dataset": "Traffic-v2-VDLive",
                "request_identity": "2025-09-20T08:00",
                "fixture": "tdx.json",
                "vd_locations": {"VD-1": [25.0, 121.5, "urban"]},
            },
            {
                "provider": "cwa",
                "dataset": "O-A0002-001",
                "request_identity": "2025-09-20T08:00",
                "fixture": "cwa.json",
            },
        ],
    }
    plan_path = _write(tmp_path / "plan.json", plan)
    snapshot_root = tmp_path / "snapshots"

    code = main(
        [
            "--repo-root", str(tmp_path),
            "--snapshot-root", str(snapshot_root),
            "snapshot",
            "--plan", str(plan_path),
        ]
    )
    assert code == 0
    manifest_path = snapshot_root / "cli-snap" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["payload_count"] == 2
    assert manifest["hino_timestamp_timezone"] == "UNRESOLVED"
    assert manifest["empirical_claim_status"] == "PROVISIONAL_NO_EMPIRICAL_CLAIM"

    # Quality over the just-written snapshot, with aware decision points.
    decision_points = [
        {"time": "2025-09-20T01:00:00+00:00", "latitude": 25.01, "longitude": 121.51}
    ]
    dp_path = _write(tmp_path / "dp.json", decision_points)
    report_path = tmp_path / "quality.json"
    code = main(
        [
            "--repo-root", str(tmp_path),
            "--snapshot-root", str(snapshot_root),
            "quality",
            "--snapshot-id", "cli-snap",
            "--decision-points", str(dp_path),
            "--output", str(report_path),
        ]
    )
    assert code == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["hino_timestamp_timezone"] == "UNRESOLVED"
    assert report["parsed_observation_count"] >= 1
    assert report["temporal_leakage"]["leakage_check_performed"] is True
    assert report["observation_integrity"]["passed"] is True


def test_cli_download_without_credentials_fails_cleanly(tmp_path, monkeypatch):
    from hino_safepilot.context.cli import main
    from hino_safepilot.context import MissingCredentialError

    monkeypatch.delenv("CWA_API_KEY", raising=False)
    plan = {
        "snapshot_id": "cli-dl",
        "entries": [
            {
                "provider": "cwa",
                "dataset": "O-A0002-001",
                "request_identity": "2025-09-20T08:00",
            }
        ],
    }
    plan_path = _write(tmp_path / "plan.json", plan)
    with pytest.raises(MissingCredentialError):
        main(
            [
                "--repo-root", str(tmp_path),
                "--snapshot-root", str(tmp_path / "snapshots"),
                "download",
                "--plan", str(plan_path),
            ]
        )
