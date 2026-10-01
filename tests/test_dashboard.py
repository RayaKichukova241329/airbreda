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