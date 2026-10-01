"""Create a least-privilege database user for the ingestion services.

Run once, locally, with the admin credentials from .env. The new password is
typed in at the prompt, so it never appears in code, logs or shell history.
"""

import getpass

from dotenv import load_dotenv
from psycopg2 import sql

from common import get_connection

load_dotenv()
password = getpass.getpass("Password for airbreda_ingest: ")

conn = get_connection()
with conn, conn.cursor() as cur:
    cur.execute(sql.SQL("CREATE ROLE airbreda_ingest WITH LOGIN PASSWORD {}")
                .format(sql.Literal(password)))
    cur.execute("GRANT CONNECT ON DATABASE postgres TO airbreda_ingest")
    cur.execute("GRANT USAGE ON SCHEMA public TO airbreda_ingest")
    cur.execute("GRANT SELECT, INSERT, UPDATE ON sensor_readings TO airbreda_ingest")
conn.close()
print("User airbreda_ingest created with SELECT, INSERT, UPDATE on sensor_readings.")