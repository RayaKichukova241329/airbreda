"""Create the sensor_readings table, or bring an existing one up to date."""

from dotenv import load_dotenv

from common import get_connection

load_dotenv()

conn = get_connection()
with conn, conn.cursor() as cur:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sensor_readings (
            station_id VARCHAR(20) NOT NULL,
            timestamp  TIMESTAMPTZ NOT NULL,
            component  VARCHAR(10) NOT NULL,
            value      FLOAT,
            PRIMARY KEY (station_id, timestamp, component)
        );
    """)
    # Day 2, Lab 2: flag stale or null Luchtmeetnet readings instead of dropping them
    cur.execute("ALTER TABLE sensor_readings ADD COLUMN IF NOT EXISTS is_flagged BOOLEAN DEFAULT FALSE;")

conn.close()
print("Table sensor_readings is ready (with is_flagged).")