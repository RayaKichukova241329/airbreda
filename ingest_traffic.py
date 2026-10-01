"""
ingest_traffic.py - fetch live NDW traffic data for the four A27/Breda sites,
save one CSV per site per hour, upload each file to Azure Blob Storage, store
valid readings in PostgreSQL and publish one message per site to Redis.

Why both a database and a bucket:
The bucket keeps the hourly traffic files exactly as they were ingested. Object
storage is cheap, the files are never modified, and they can be replayed through
updated code at any time. This matters because the NDW feed only shows current
traffic: data that was not saved when it was live cannot be recovered later.
The database holds cleaned, structured readings with a primary key that prevents
duplicates. It supports fast queries, filters and JOINs between air quality and
traffic data, which the dashboard and the model depend on.

Retraining the ML model six months from now: the database only contains the
readings as processed by the code that was running at the time. If the model
needs a new feature, or a parsing bug is discovered, the files in the bucket are
replayed through the updated code to rebuild a correct training set. The
database alone cannot provide this, because anything it never stored, or stored
incorrectly, would be lost.

Bad data: NDW reports speed = -1 when a lane has no valid speed measurement.
This is a sentinel value, not a speed, so a site's speed reading is never
written to the database when any of its lanes reports -1. The event is logged
as a WARNING and counted. The flow reading and the bucket file are still kept,
so nothing is lost from the raw record.

Polling interval: once per hour (POLL_INTERVAL_SECONDS=3600 in docker-compose.yml).
Each run downloads NDW's nationwide feeds, which are large, only to extract four
sites. Polling every minute instead would:
- download the national feeds 60 times per hour, and runs could overlap because
  a single download can take up to a minute;
- overwrite each hourly file 59 times, because file names only go down to the
  hour, so the bucket would still hold just one snapshot per hour;
- push 240 messages per hour onto the queue instead of 4.
Trade-off: NDW measures every minute, so minute polling would capture traffic
more completely, while one snapshot per hour is a noisy sample (see ADR-001).
If the model needs better traffic data, the right fix is to aggregate the
minute values into hourly averages, which also requires a new file layout.
"""

import io
import json
import logging
import os
import sys
from pathlib import Path

import pandas as pd
import psycopg2
import redis
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv
from psycopg2.extras import execute_values

from common import BadDataCounter, get_connection, log_event, run, setup_logging, utc_now_iso
from getTrafficReadings import (
    CONFIG_URL,
    MEASURED_URL,
    build_index_map,
    download_and_decompress,
    extract_measurements,
)

SOURCE = "NDW"

# Short name used in file names and as station_id -> full NDW measurement site ID
SITES = {
    "hrl": "RWS01_MONIBAS_0271hrl0063ra",  # A27 mainline, direction 1
    "hrr": "RWS01_MONIBAS_0271hrr0063ra",  # A27 mainline, direction 2
    "vwd": "RWS01_MONIBAS_0270vwd0063ra",  # entry slip road (leaving Breda)
    "vwa": "RWS01_MONIBAS_0270vwa0063ra",  # exit slip road (entering Breda)
}

LOCAL_DIR = Path("data")
REDIS_LIST = "readings"
# 0 (the default) means: run once and exit. docker-compose.yml sets 3600.
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "0"))
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "0"))

# In-memory health state, reported on /health. Resets when the service restarts.
ndw_bad_data_count = BadDataCounter(SOURCE)
last_successful_fetch = None


def health_status() -> dict:
    return {
        "last_successful_fetch": last_successful_fetch,
        "bad_data_count": ndw_bad_data_count.count,
        "source": SOURCE,
    }


def parse_site(config_bytes: bytes, measured_bytes: bytes, short_name: str) -> dict:
    """Return one summary row (total flow, average speed, timestamp) for a site."""
    site_id = SITES[short_name]

    index_map = build_index_map(io.BytesIO(config_bytes), site_id)
    flow_idx = {i for i, info in index_map.items()
                if info["type"] == "trafficFlow" and info["vehicle"] == "anyVehicle"}
    speed_idx = {i for i, info in index_map.items()
                 if info["type"] == "trafficSpeed" and info["vehicle"] == "anyVehicle"}

    readings = extract_measurements(io.BytesIO(measured_bytes), site_id)
    flows = [float(r["value"]) for r in readings if r["index"] in flow_idx]
    raw_speeds = [float(r["value"]) for r in readings if r["index"] in speed_idx]
    speeds = [s for s in raw_speeds if s > 0]

    return {
        "site": short_name,
        "site_id": site_id,
        "timestamp": readings[0]["timestamp"] if readings else None,
        "lanes": len(flow_idx),
        "total_flow": sum(flows),
        "avg_speed": sum(speeds) / len(speeds) if speeds else None,
        "invalid_speed_lanes": sum(1 for s in raw_speeds if s == -1),
    }


def rows_for_database(row: dict) -> list[tuple]:
    """Turn one site's summary into sensor_readings rows, dropping bad speed data.

    Returns (station_id, timestamp, component, value) tuples. When any lane
    reported speed = -1, the speed row is skipped, a DATA_QUALITY_ERROR warning
    is logged and ndw_bad_data_count is incremented.
    """
    rows = [(row["site"], row["timestamp"], "flow", float(row["total_flow"]))]

    if row["invalid_speed_lanes"] > 0:
        log_event(logging.WARNING, "DATA_QUALITY_ERROR",
                  source=SOURCE, location=row["site_id"], field="speed", value=-1,
                  lanes_affected=row["invalid_speed_lanes"], timestamp=row["timestamp"])
        ndw_bad_data_count.increment()
    elif row["avg_speed"] is not None:
        rows.append((row["site"], row["timestamp"], "speed", float(row["avg_speed"])))

    return rows


def write_traffic_readings(rows: list[tuple]) -> int:
    """Insert rows into sensor_readings (idempotent). Returns how many were NEW."""
    if not rows:
        return 0
    conn = get_connection()
    try:
        with conn, conn.cursor() as cur:
            inserted = execute_values(
                cur,
                """
                INSERT INTO sensor_readings (station_id, timestamp, component, value)
                VALUES %s
                ON CONFLICT (station_id, timestamp, component) DO NOTHING
                RETURNING 1
                """,
                rows,
                fetch=True,
            )
    finally:
        conn.close()
    return len(inserted)


def blob_path_for(row: dict) -> str:
    """Build ndw/YYYY-MM-DD/HH-<site>.csv from the MEASUREMENT time (UTC)."""
    ts = pd.to_datetime(row["timestamp"], utc=True)
    return f"ndw/{ts:%Y-%m-%d}/{ts:%H}-{row['site']}.csv"


def save_and_upload(row: dict, container) -> str:
    """Save the row as a local CSV, then upload it to the bucket under the same path."""
    path = blob_path_for(row)
    local_file = LOCAL_DIR / path
    local_file.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([row]).to_csv(local_file, index=False)

    with open(local_file, "rb") as f:
        container.upload_blob(name=path, data=f, overwrite=True)
    return path


def publish_site(row: dict, client: redis.Redis) -> None:
    """Push one JSON message for this site onto the Redis 'readings' list."""
    message = {
        "source": "ndw",
        "station_id": row["site"],
        "site_id": row["site_id"],
        "timestamp": row["timestamp"],
        "total_flow": row["total_flow"],
        "avg_speed": row["avg_speed"],
        "invalid_speed_lanes": row["invalid_speed_lanes"],
    }
    client.rpush(REDIS_LIST, json.dumps(message))


def main() -> int:
    global last_successful_fetch
    load_dotenv()

    # 1. Download both NDW feeds once, then reuse them for all four sites
    try:
        config_bytes = download_and_decompress(CONFIG_URL).read()
        measured_bytes = download_and_decompress(MEASURED_URL).read()
    except Exception as e:
        log_event(logging.ERROR, "fetch_failed", source=SOURCE, error=str(e))
        return 1

    # 2. Connect to the bucket and, if configured, to Redis
    service = BlobServiceClient.from_connection_string(
        os.environ["AZURE_STORAGE_CONNECTION_STRING"]
    )
    container = service.get_container_client(os.environ["AZURE_STORAGE_CONTAINER"])
    redis_host = os.environ.get("REDIS_HOST")
    client = redis.Redis(host=redis_host, port=6379) if redis_host else None

    # 3. Parse, save, upload and publish each site; collect rows for the database
    failures = 0
    db_rows = []
    for short_name in SITES:
        row = parse_site(config_bytes, measured_bytes, short_name)
        if row["timestamp"] is None:
            log_event(logging.ERROR, "fetch_failed", source=SOURCE,
                      location=SITES[short_name], reason="no readings for site")
            failures += 1
            continue

        log_event(logging.INFO, "fetch_success", source=SOURCE, station_id=short_name,
                  location=row["site_id"], total_flow=row["total_flow"],
                  avg_speed=row["avg_speed"], timestamp=row["timestamp"])
        db_rows.extend(rows_for_database(row))

        # The bucket keeps every reading, including ones with bad speed data
        try:
            path = save_and_upload(row, container)
            log_event(logging.INFO, "upload_success", source=SOURCE,
                      station_id=short_name, path=path)
        except Exception as e:
            log_event(logging.ERROR, "upload_failed", source=SOURCE,
                      station_id=short_name, error=str(e))
            failures += 1

        if client is not None:
            try:
                publish_site(row, client)
            except redis.RedisError as e:
                log_event(logging.ERROR, "publish_failed", source=SOURCE,
                          station_id=short_name, error=str(e))
                failures += 1

    if failures < len(SITES):
        last_successful_fetch = utc_now_iso()

    # 4. Store the valid readings in one transaction (idempotent)
    try:
        inserted = write_traffic_readings(db_rows)
        log_event(logging.INFO, "db_write_success", source=SOURCE,
                  inserted=inserted, skipped=len(db_rows) - inserted)
    except psycopg2.Error as e:
        log_event(logging.ERROR, "db_write_failed", source=SOURCE, error=str(e))
        failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    setup_logging()
    sys.exit(run(main, POLL_INTERVAL_SECONDS, HEALTH_PORT, health_status))