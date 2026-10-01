"""
build_training_data.py - join NO2 readings (database) with traffic files (bucket)
into one training table, training_data.csv.

Time alignment: a Luchtmeetnet value stamped T is the AVERAGE over the hour
T-1h to T. An NDW snapshot taken at 15:22 falls inside the hour 15:00-16:00, so
it belongs to the NO2 value stamped 16:00. Rounding both to the nearest hour
would instead pair it with the value stamped 15:00, the average of the hour
BEFORE the traffic happened. Both sides are therefore keyed on the START of
their hour (hour_start).

Data quality: only NO2 rows that are not null and not flagged are used. Flagged
rows stay in the database for transparency, but must not teach the model.
Hours where any of the four NDW sites is missing are dropped, because the total
intensity would be incomplete.
"""

import io
import logging
import re

import pandas as pd
from dotenv import load_dotenv

from common import get_connection, get_container_client, log_event, setup_logging
from features import hour_of_day

STATION = "NL10240"
SITES = ["hrl", "hrr", "vwd", "vwa"]
OUTPUT = "training_data.csv"
BLOB_PATTERN = re.compile(r"^ndw/(\d{4}-\d{2}-\d{2})/(\d{2})-(hrl|hrr|vwd|vwa)\.csv$")


def load_no2(conn) -> pd.DataFrame:
    """Clean hourly NO2 readings, keyed on the start of their measurement hour."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT timestamp, value
            FROM sensor_readings
            WHERE station_id = %s AND component = 'NO2'
              AND value IS NOT NULL AND NOT COALESCE(is_flagged, FALSE)
            ORDER BY timestamp
            """,
            (STATION,),
        )
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["timestamp", "no2_ug_m3"])
    df["hour_start"] = pd.to_datetime(df["timestamp"], utc=True) - pd.Timedelta(hours=1)
    return df[["hour_start", "no2_ug_m3"]]


def load_traffic(container) -> pd.DataFrame:
    """Read every hourly NDW file in the bucket: one row per site per hour."""
    records = []
    for blob in container.list_blobs(name_starts_with="ndw/"):
        match = BLOB_PATTERN.match(blob.name)
        if not match:
            continue  # not one of our hourly site files
        date, hour, site = match.groups()
        try:
            content = container.download_blob(blob.name).readall()
            csv = pd.read_csv(io.BytesIO(content))
            records.append({
                "hour_start": pd.Timestamp(f"{date}T{hour}:00:00Z"),
                "site": site,
                "intensity": float(csv["total_flow"].iloc[0]),
            })
        except Exception as e:  # one unreadable file should not stop the build
            log_event(logging.WARNING, "training_file_skipped", path=blob.name, error=str(e))
    return pd.DataFrame(records, columns=["hour_start", "site", "intensity"])


def build_dataset(no2: pd.DataFrame, traffic: pd.DataFrame) -> pd.DataFrame:
    """Pivot traffic per site, join it to NO2 on hour_start and add the features."""
    per_site = traffic.pivot_table(index="hour_start", columns="site",
                                   values="intensity", aggfunc="last")
    per_site = per_site.reindex(columns=SITES).dropna()  # all four sites required

    df = no2.merge(per_site, left_on="hour_start", right_index=True, how="inner")
    df["total_intensity_veh_per_hr"] = df[SITES].sum(axis=1)
    df["hour_of_day"] = df["hour_start"].apply(hour_of_day)
    df = df.sort_values("hour_start").reset_index(drop=True)
    return df[["hour_start", "no2_ug_m3", *SITES, "total_intensity_veh_per_hr", "hour_of_day"]]


def main() -> None:
    load_dotenv()
    conn = get_connection()
    try:
        no2 = load_no2(conn)
    finally:
        conn.close()
    traffic = load_traffic(get_container_client(source="training"))

    df = build_dataset(no2, traffic)
    df.to_csv(OUTPUT, index=False)
    log_event(logging.INFO, "training_data_built", rows=len(df),
              no2_hours=len(no2), traffic_hours=traffic["hour_start"].nunique(),
              first_hour=df["hour_start"].min() if len(df) else None,
              last_hour=df["hour_start"].max() if len(df) else None,
              output=OUTPUT)


if __name__ == "__main__":
    setup_logging()
    main()