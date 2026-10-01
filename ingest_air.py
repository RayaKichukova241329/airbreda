"""
ingest_air.py - fetch NO2 readings for Luchtmeetnet station NL10240, store them
in PostgreSQL and publish them to the Redis 'readings' queue.

CAP trade-off: the Luchtmeetnet sensor network is an AP system. When a station
loses its connection, the API keeps responding with the last available data
instead of returning an error, so it favours availability over consistency.
As a result, a successful response does not guarantee that the data is current.

Handling missing data in a production pipeline:
- Empty result: the API returns HTTP 200 with an empty list when there is no
  data. The pipeline logs an error and exits with a non-zero code, so the
  failure is visible to monitoring instead of passing silently.
- Null value: a record with a null value is kept, not dropped, and stored with
  is_flagged = TRUE. The row shows that the hour existed but the sensor
  delivered no measurement. Dropping it would hide the gap.
- Stale value: a value that stays exactly the same for 3 or more consecutive
  hours suggests a stuck or interpolated sensor. All rows in such a run are
  kept and flagged, because we cannot tell which of them (if any) is genuine.
- Stale feed: the pipeline compares the latest timestamp with the current time.
  Readings normally arrive about one hour late, so a reading older than three
  hours indicates that the station may be offline.

Polling interval: once per hour (POLL_INTERVAL_SECONDS=3600 in docker-compose.yml).
Luchtmeetnet publishes one averaged value per hour, usually within an hour after
the measurement window closes, so polling more often cannot return fresher data.
Polling every minute instead would stay within the API's fair-use limit
(100 requests per 5 minutes), but it would:
- make 60 API calls per hour that return the same data 59 times;
- push the same readings onto the queue 60 times per hour (about 3,000 messages
  instead of 50), flooding Redis and every future consumer with duplicates;
- run 60 database writes per hour that insert nothing new.
Nothing becomes more accurate; only load and duplicate traffic increase.
"""

import json
import logging
import os
import sys
import time

import pandas as pd
import psycopg2
import redis
import requests
from dotenv import load_dotenv
from psycopg2.extras import execute_values

from common import BadDataCounter, get_connection, log_event, run, setup_logging, utc_now_iso

SOURCE = "Luchtmeetnet"
STATION = "NL10240"
URL = f"https://api.luchtmeetnet.nl/open_api/stations/{STATION}/measurements"
STALE_AFTER = pd.Timedelta(hours=3)
STALE_RUN_LENGTH = 3  # identical values for this many consecutive hours = stale

REDIS_LIST = "readings"
# 0 (the default) means: run once and exit. docker-compose.yml sets 3600.
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "0"))
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "0"))

# In-memory health state, reported on /health. Resets when the service restarts.
luchtmeetnet_bad_data_count = BadDataCounter(SOURCE)
last_successful_fetch = None


def health_status() -> dict:
    return {
        "last_successful_fetch": last_successful_fetch,
        "bad_data_count": luchtmeetnet_bad_data_count.count,
        "source": SOURCE,
    }


def fetch_measurements(formula: str = "NO2") -> list[dict]:
    """Return one page of measurements (newest first) as a list of dicts."""
    params = {
        "formula": formula,
        "order_by": "timestamp_measured",
        "order_direction": "desc",
        "page": 1,
    }
    response = requests.get(URL, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()
    return data.get("data", [])


def to_dataframe(measurements: list[dict]) -> pd.DataFrame:
    """Turn the API records into a DataFrame with the columns the test expects."""
    df = pd.DataFrame(measurements, columns=["formula", "value", "timestamp_measured"])
    return df.rename(columns={"formula": "component", "timestamp_measured": "timestamp"})


def filter_no2_readings(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only NO2 rows. Rows with a null value must be KEPT, not dropped."""
    return df[df["component"] == "NO2"]


def flag_readings(df: pd.DataFrame) -> pd.DataFrame:
    """Add is_flagged and flag_reason columns.

    A row is flagged when its value is null, or when it belongs to a run of
    STALE_RUN_LENGTH or more consecutive hourly readings with exactly the same
    value. A missing hour or a null value breaks a run.
    """
    out = df.copy()
    out["is_flagged"] = False
    out["flag_reason"] = None
    ordered = out.assign(_ts=pd.to_datetime(out["timestamp"], utc=True)).sort_values("_ts")

    def close_run(run_indices):
        if len(run_indices) >= STALE_RUN_LENGTH:
            out.loc[run_indices, "is_flagged"] = True
            out.loc[run_indices, "flag_reason"] = "unchanged_3h"

    run_indices, prev_ts, prev_value = [], None, None
    for idx, row in ordered.iterrows():
        value, ts = row["value"], row["_ts"]
        if pd.isna(value):
            close_run(run_indices)
            run_indices, prev_ts, prev_value = [], None, None
            out.loc[idx, "is_flagged"] = True
            out.loc[idx, "flag_reason"] = "null"
            continue
        if run_indices and value == prev_value and ts - prev_ts == pd.Timedelta(hours=1):
            run_indices.append(idx)
        else:
            close_run(run_indices)
            run_indices = [idx]
        prev_ts, prev_value = ts, value
    close_run(run_indices)
    return out


def write_readings(df: pd.DataFrame) -> list[dict]:
    """Insert readings into sensor_readings and return the rows that changed.

    New rows are inserted. An existing row is only touched when it now needs to
    be flagged (for example, the third identical hour arrived and completed a
    stale run); a flag is never removed. Rows that are already stored exactly
    like this are skipped, so running the script twice changes nothing.
    """
    rows = [
        (STATION, r.timestamp, r.component,
         None if pd.isna(r.value) else float(r.value), bool(r.is_flagged))
        for r in df.itertuples(index=False)
    ]
    conn = get_connection()
    try:
        with conn, conn.cursor() as cur:
            changed = execute_values(
                cur,
                """
                INSERT INTO sensor_readings (station_id, timestamp, component, value, is_flagged)
                VALUES %s
                ON CONFLICT (station_id, timestamp, component) DO UPDATE
                    SET is_flagged = TRUE
                    WHERE EXCLUDED.is_flagged AND NOT sensor_readings.is_flagged
                RETURNING timestamp, is_flagged, (xmax = 0) AS inserted
                """,
                rows,
                fetch=True,
            )
    finally:
        conn.close()
    # xmax = 0 is PostgreSQL's way of telling an inserted row from an updated one
    return [{"timestamp": ts, "is_flagged": flagged, "inserted": inserted}
            for ts, flagged, inserted in changed]


def report_bad_rows(flagged_df: pd.DataFrame, changed: list[dict]) -> None:
    """Log one WARNING per flagged row that is new or newly flagged.

    Rows the database already had are not reported again, so the same bad hour
    is counted once, not once per hourly run for the next 50 hours.
    """
    changed_flagged = {pd.to_datetime(c["timestamp"], utc=True) for c in changed if c["is_flagged"]}
    for r in flagged_df[flagged_df["is_flagged"]].itertuples(index=False):
        if pd.to_datetime(r.timestamp, utc=True) in changed_flagged:
            log_event(logging.WARNING, "DATA_QUALITY_ERROR",
                      source=SOURCE, station_id=STATION, field=r.component,
                      reason="stale_or_null", detail=r.flag_reason,
                      value=None if pd.isna(r.value) else float(r.value),
                      timestamp=r.timestamp)
            luchtmeetnet_bad_data_count.increment()


def publish_readings(df: pd.DataFrame, client: redis.Redis) -> int:
    """Push every reading as a JSON message onto the Redis 'readings' list."""
    pipe = client.pipeline()
    for r in df.itertuples(index=False):
        message = {
            "source": "luchtmeetnet",
            "station_id": STATION,
            "timestamp": r.timestamp,
            "component": r.component,
            "value": None if pd.isna(r.value) else float(r.value),
            "is_flagged": bool(r.is_flagged),
        }
        pipe.rpush(REDIS_LIST, json.dumps(message))
    pipe.execute()
    return len(df)


def main() -> int:
    global last_successful_fetch
    load_dotenv()

    # 1. Fetch, retrying with exponential backoff if the API is briefly unreachable
    measurements = None
    for attempt in range(1, 4):
        try:
            measurements = fetch_measurements()
            break
        except requests.exceptions.RequestException as e:
            log_event(logging.WARNING, "fetch_retry", source=SOURCE,
                      station_id=STATION, attempt=attempt, error=str(e))
            time.sleep(2 ** attempt)  # wait 2s, then 4s, then 8s

    if measurements is None:
        log_event(logging.ERROR, "fetch_failed", source=SOURCE, station_id=STATION,
                  reason="API unreachable after 3 attempts")
        return 1

    # 2. Clean, filter and flag
    df = filter_no2_readings(to_dataframe(measurements))
    if df.empty:
        log_event(logging.ERROR, "fetch_failed", source=SOURCE, station_id=STATION,
                  reason="API returned no NO2 measurements")
        return 1
    df = flag_readings(df)

    # 3. Successful fetch: record it for /health and log the latest reading
    last_successful_fetch = utc_now_iso()
    latest = df.iloc[0]
    log_event(logging.INFO, "fetch_success", source=SOURCE, station_id=STATION,
              value=None if pd.isna(latest["value"]) else float(latest["value"]),
              timestamp=latest["timestamp"], readings_returned=len(df))

    # 4. Warn if the feed itself is stale (no new hour for too long)
    age = pd.Timestamp.now(tz="UTC") - pd.to_datetime(latest["timestamp"])
    if age > STALE_AFTER:
        log_event(logging.WARNING, "stale_feed", source=SOURCE, station_id=STATION,
                  latest_timestamp=latest["timestamp"],
                  age_hours=round(age.total_seconds() / 3600, 1))

    failed = False

    # 5. Store the readings (idempotent) and report bad rows that are new
    try:
        changed = write_readings(df)
        report_bad_rows(df, changed)
        inserted = sum(c["inserted"] for c in changed)
        log_event(logging.INFO, "db_write_success", source=SOURCE, station_id=STATION,
                  inserted=inserted, newly_flagged=len(changed) - inserted,
                  unchanged=len(df) - len(changed))
    except psycopg2.Error as e:
        log_event(logging.ERROR, "db_write_failed", source=SOURCE, error=str(e))
        failed = True

    # 6. Publish to the queue. Independent of step 5: a database problem
    #    should not stop other consumers from receiving the readings.
    redis_host = os.environ.get("REDIS_HOST")
    if redis_host:
        try:
            published = publish_readings(df, redis.Redis(host=redis_host, port=6379))
            log_event(logging.INFO, "publish_success", source=SOURCE,
                      queue=REDIS_LIST, messages=published)
        except redis.RedisError as e:
            log_event(logging.ERROR, "publish_failed", source=SOURCE, error=str(e))
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    setup_logging()
    sys.exit(run(main, POLL_INTERVAL_SECONDS, HEALTH_PORT, health_status))