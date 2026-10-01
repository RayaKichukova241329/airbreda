"""Tests for the data quality handlers (Day 2, Lab 2).

The database is replaced by a fake, so these tests run anywhere, including in
CI where the real Azure database is not reachable.
"""

import pandas as pd

import ingest_air
import ingest_traffic


class FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return FakeCursor()

    def close(self):
        pass


def test_null_luchtmeetnet_reading_is_written_with_flag(monkeypatch):
    written = {}

    def fake_execute_values(cur, sql, rows, fetch):
        written["rows"] = rows
        # pretend every row was new: (timestamp, is_flagged, inserted)
        return [(r[1], r[4], True) for r in rows]

    monkeypatch.setattr(ingest_air, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(ingest_air, "execute_values", fake_execute_values)

    df = pd.DataFrame({
        "component": ["NO2", "NO2"],
        "value": [18.4, None],
        "timestamp": ["2024-01-15T09:00:00+00:00", "2024-01-15T08:00:00+00:00"],
    })
    ingest_air.write_readings(ingest_air.flag_readings(df))

    rows = {r[1]: r for r in written["rows"]}  # by timestamp
    null_row = rows["2024-01-15T08:00:00+00:00"]
    assert null_row[3] is None   # stored as NULL, not dropped
    assert null_row[4] is True   # is_flagged = TRUE
    assert rows["2024-01-15T09:00:00+00:00"][4] is False


def test_value_unchanged_for_three_hours_is_flagged():
    df = pd.DataFrame({
        "component": ["NO2"] * 4,
        "value": [21.0, 30.0, 30.0, 30.0],
        "timestamp": [f"2024-01-15T{h:02d}:00:00+00:00" for h in (8, 7, 6, 5)],
    })
    flagged = ingest_air.flag_readings(df).set_index("timestamp")["is_flagged"]

    assert not flagged["2024-01-15T08:00:00+00:00"]
    assert flagged[["2024-01-15T07:00:00+00:00", "2024-01-15T06:00:00+00:00",
                    "2024-01-15T05:00:00+00:00"]].all()


def test_ndw_speed_minus_one_is_not_written_and_counted():
    before = ingest_traffic.ndw_bad_data_count.count
    row = {
        "site": "hrl",
        "site_id": "RWS01_MONIBAS_0271hrl0063ra",
        "timestamp": "2024-01-15T08:00:00Z",
        "lanes": 3,
        "total_flow": 1200.0,
        "avg_speed": 95.0,
        "invalid_speed_lanes": 1,
    }
    rows = ingest_traffic.rows_for_database(row)

    components = [r[2] for r in rows]
    assert "speed" not in components   # the bad speed row is not written
    assert "flow" in components        # the valid flow reading is kept
    assert ingest_traffic.ndw_bad_data_count.count == before + 1