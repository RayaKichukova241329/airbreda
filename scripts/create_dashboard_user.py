"""Create a read-only database user for the dashboard.

The dashboard only reads sensor_readings, so its user can only SELECT. Even if
the public-facing dashboard were compromised, this user could not change or
delete any data.

Run once, locally, with the admin credentials from .env. The new password is
typed in at the prompt, so it never appears in code, logs or shell history.
"""

import getpass

from dotenv import load_dotenv
from psycopg2 import sql

from common import get_connection

load_dotenv()
password = getpass.getpass("Password for airbreda_dashboard: ")

conn = get_connection()
with conn, conn.cursor() as cur:
    cur.execute(sql.SQL("CREATE ROLE airbreda_dashboard WITH LOGIN PASSWORD {}")
                .format(sql.Literal(password)))
    cur.execute("GRANT CONNECT ON DATABASE postgres TO airbreda_dashboard")
    cur.execute("GRANT USAGE ON SCHEMA public TO airbreda_dashboard")
    cur.execute("GRANT SELECT ON sensor_readings TO airbreda_dashboard")
conn.close()
print("User airbreda_dashboard created with SELECT on sensor_readings only.")