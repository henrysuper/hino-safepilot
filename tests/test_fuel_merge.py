"""Merge integration checks using synthetic inputs only."""

import json
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

pd = pytest.importorskip("pandas")
pytest.importorskip("pyproj")

from hino_safepilot.fuel.dispatch_health import HealthPolicy, apply_health_gate
from hino_safepilot.fuel.server import make_handler
from hino_safepilot.fuel import server as console_server


def test_health_gate_excludes_unhealthy_vehicle_on_heavy_task():
    options = [
        {"vehicle_id": "synthetic-low", "predicted_fuel_L": 10.0},
        {"vehicle_id": "synthetic-good", "predicted_fuel_L": 20.0},
    ]
    result = apply_health_gate(
        options, pd.Series({"synthetic-low": 50.0, "synthetic-good": 90.0}),
        100.0, HealthPolicy(mode="absolute"),
    )
    assert result[0]["vehicle_id"] == "synthetic-good"
    assert result[1]["health_excluded"] is True
    assert "health_excluded" not in options[0]


def test_dashboard_serves_bundled_file(tmp_path, monkeypatch):
    monkeypatch.setattr(console_server, "WEB_DIR", tmp_path)
    state = SimpleNamespace(paths=SimpleNamespace(repo_root=tmp_path))
    # do_GET constructs the API route table before serving static files.
    for name in ("meta_payload", "backtest", "scores", "fences", "fence_test", "drivers_payload"):
        setattr(state, name, lambda: {})
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/dashboard_data.json"
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(url)
        assert error.value.code == 404
        target = tmp_path / "dashboard_data.json"
        target.write_text('{"synthetic": true}', encoding="utf-8")
        with urlopen(url) as response:
            assert json.load(response) == {"synthetic": True}
            assert response.headers["Cache-Control"] == "no-store"
        with pytest.raises(HTTPError):
            urlopen(f"http://127.0.0.1:{server.server_port}/data/processed/fuel/imported_dashboard/dashboard_data.json")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
