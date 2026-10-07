from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"
SAMPLES = DEMO / "sample-data"

FORBIDDEN_PAYLOAD_KEYS = {
    "carnum",
    "enabledcode",
    "enabled_code",
    "journeycode",
    "journey_code",
    "trip_id",
    "latitude",
    "longitude",
    "raw_source_row",
    "source_row",
}


def _walk_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        keys = {str(key).casefold() for key in value}
        for child in value.values():
            keys.update(_walk_keys(child))
        return keys
    if isinstance(value, list):
        keys: set[str] = set()
        for child in value:
            keys.update(_walk_keys(child))
        return keys
    return set()


def test_demo_has_all_static_resources() -> None:
    for relative in ("index.html", "styles.css", "app.js", "README.md"):
        assert (DEMO / relative).is_file()


def test_demo_sample_payloads_are_valid_and_synthetic() -> None:
    expected = {"driving", "drivers", "driver"}
    observed: set[str] = set()

    for path in sorted(SAMPLES.glob("*.synthetic.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        observed.add(payload["view"])
        assert payload["data_classification"] == "synthetic"
        assert not (_walk_keys(payload) & FORBIDDEN_PAYLOAD_KEYS)

    assert observed == expected


def test_demo_tree_contains_no_forbidden_identifier_names() -> None:
    searchable = [
        *DEMO.glob("*.html"),
        *DEMO.glob("*.css"),
        *DEMO.glob("*.js"),
        *SAMPLES.glob("*.json"),
    ]
    joined = "\n".join(path.read_text(encoding="utf-8").casefold() for path in searchable)
    for forbidden in FORBIDDEN_PAYLOAD_KEYS:
        assert forbidden not in joined


def test_demo_discloses_offline_experiment_and_synthetic_preview() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (DEMO / "index.html", DEMO / "app.js")
    )
    assert "Model Status: XGBoost Experiment Ready · Not Production" in text
    assert "Evaluation Mode: Offline" in text
    assert "Synthetic Demo" in text
    assert "未連接即時車輛" in text
    assert "Alert Policy V1 · Synthetic Timeline" in text
