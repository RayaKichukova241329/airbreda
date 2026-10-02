"""Tests for the dashboard API (Day 4, Lab 2).

The database and the bucket are replaced by fixed test data, so these tests run
anywhere, including in CI where the real cloud resources are not reachable.
"""

import pytest
from fastapi.testclient import TestClient

import dashboard

NO2 = {"no2_ug_m3": 18.4, "no2_timestamp": "2026-10-01T16:00:00Z", "no2_is_flagged": False}
TRAFFIC = {site: {"intensity_veh_per_hr": value, "timestamp": "2026-10-01T15:22:00Z"}
           for site, value in zip(dashboard.SITES, [2000.0, 1500.0, 400.0, 500.0])}


@pytest.fixture
def client(monkeypatch):
    dashboard._cache.clear()
    monkeypatch.setattr(dashboard, "load_latest_no2", lambda: NO2)
    monkeypatch.setattr(dashboard, "load_latest_traffic", lambda: TRAFFIC)
    return TestClient(dashboard.app)


def test_site_returns_the_day5_contract(client):
    body = client.get("/site/hrl").json()

    assert body["site_id"] == "hrl"
    assert body["no2_ug_m3"] == 18.4
    assert body["intensity_veh_per_hr"] == 2000.0
    assert 0 <= body["no2_exceedance_risk"] <= 1
    assert body["timestamp"] == "2026-10-01T15:22:00Z"
    assert body["total_intensity_veh_per_hr"] == 4400.0


def test_prediction_failure_still_returns_real_values(client, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("model.pkl is corrupt")

    monkeypatch.setattr(dashboard.model, "predict", broken)
    response = client.get("/site/vwa")

    assert response.status_code == 200
    body = response.json()
    assert body["no2_ug_m3"] == 18.4               # real data is still there
    assert body["no2_exceedance_risk"] is None      # prediction is clearly missing
    assert "prediction_error" in body


def test_unknown_site_is_rejected(client):
    assert client.get("/site/xyz").status_code == 404


def test_history_aligns_no2_with_the_hour_it_averages():
    import pandas as pd

    end = pd.Timestamp("2026-10-01T16:00:00Z")
    # NO2 stamped 16:00 is the average of 15:00-16:00
    no2_rows = [(pd.Timestamp("2026-10-01T16:00:00Z"), 18.4, False)]
    # traffic measured during 15:00-16:00, all four sites; 14:00 has only one site
    flow_rows = [(pd.Timestamp("2026-10-01T15:00:00"), site, 1000.0) for site in dashboard.SITES]
    flow_rows.append((pd.Timestamp("2026-10-01T14:00:00"), "hrl", 900.0))

    points = {p["hour_start"]: p for p in dashboard.build_history(no2_rows, flow_rows, end, 3)}

    assert points["2026-10-01T15:00:00Z"]["no2_ug_m3"] == 18.4
    assert points["2026-10-01T15:00:00Z"]["total_intensity_veh_per_hr"] == 4000.0
    assert points["2026-10-01T14:00:00Z"]["total_intensity_veh_per_hr"] is None  # incomplete hour
    assert len(points) == 4  # 13:00, 14:00, 15:00 and 16:00


def test_history_rejects_out_of_range_hours(client):
    assert client.get("/history?hours=0").status_code == 422
    assert client.get("/history?hours=1000").status_code == 422


def test_model_info_reports_data_volume_and_error(client, monkeypatch, tmp_path):
    import json

    metrics = tmp_path / "model_metrics.json"
    metrics.write_text(json.dumps({
        "trained_at": "2026-10-01T17:16:22+00:00", "rows": 6,
        "first_hour": "2026-09-30 21:00:00+00:00", "last_hour": "2026-10-01 15:00:00+00:00",
        "mae_leave_one_out": 6.07, "coefficients": {"total_intensity_veh_per_hr": 0.000851},
    }))
    monkeypatch.setattr(dashboard, "METRICS_PATH", metrics)
    body = client.get("/model").json()

    assert body["rows"] == 6
    assert body["mae_leave_one_out"] == 6.07
    assert body["no2_change_per_1000_veh_per_hr"] == 0.85


def test_history_includes_a_prediction_only_where_traffic_is_complete():
    import pandas as pd

    end = pd.Timestamp("2026-10-01T16:00:00Z")
    no2_rows = [(pd.Timestamp("2026-10-01T16:00:00Z"), 18.4, False)]
    flow_rows = [(pd.Timestamp("2026-10-01T15:00:00"), site, 1000.0) for site in dashboard.SITES]

    points = {p["hour_start"]: p for p in dashboard.build_history(no2_rows, flow_rows, end, 2)}

    assert isinstance(points["2026-10-01T15:00:00Z"]["no2_ug_m3_predicted"], float)
    assert points["2026-10-01T14:00:00Z"]["no2_ug_m3_predicted"] is None