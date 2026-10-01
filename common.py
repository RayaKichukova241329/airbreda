"""
common.py - shared helpers for the AirBreda ingestion services.

Both services use the same structured logging format, the same bad-data counter
and the same /health endpoint, so their output can be queried and compared in
one place (AWS Well-Architected: standardise telemetry across the workload).
Secrets such as database passwords and connection strings are never logged.
"""

import json
import logging
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg2


def setup_logging() -> None:
    """Send every log record to stdout as a single line (the JSON itself)."""
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        stream=sys.stdout, force=True)
    # Third-party libraries only log warnings and errors, so stdout stays pure JSON
    logging.getLogger("azure").setLevel(logging.WARNING)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def log_event(level: int, event: str, **fields) -> None:
    """Log one structured JSON event. Every event has the same base fields."""
    record = {
        "logged_at": utc_now_iso(),
        "level": logging.getLevelName(level),
        "event": event,
        **fields,
    }
    logging.log(level, json.dumps(record, default=str))


class BadDataCounter:
    """Counts DATA_QUALITY_ERROR events for one source.

    `count` is the running total shown on /health. Separately, the counter keeps
    the timestamps of recent events, and logs one BAD_DATA_THRESHOLD_EXCEEDED
    error when more than `threshold` events happen within `window_seconds`.
    After alerting, it stays quiet for one window so the log is not flooded.
    """

    def __init__(self, source: str, threshold: int = 10, window_seconds: int = 3600,
                 clock=time.monotonic):
        self.source = source
        self.threshold = threshold
        self.window = window_seconds
        self._clock = clock
        self.count = 0
        self._recent = deque()
        self._last_alert = None

    def increment(self) -> None:
        now = self._clock()
        self.count += 1
        self._recent.append(now)
        while self._recent and now - self._recent[0] > self.window:
            self._recent.popleft()

        in_window = len(self._recent)
        recently_alerted = self._last_alert is not None and now - self._last_alert <= self.window
        if in_window > self.threshold and not recently_alerted:
            log_event(logging.ERROR, "BAD_DATA_THRESHOLD_EXCEEDED",
                      source=self.source, count=in_window)
            self._last_alert = now


def start_health_server(port: int, get_status) -> ThreadingHTTPServer:
    """Serve GET /health in a background thread, returning get_status() as JSON."""

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.rstrip("/") != "/health":
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(get_status()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass  # silence the default plain-text access log; our logs are JSON only

    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log_event(logging.INFO, "health_server_started", port=port)
    return server


def get_connection():
    """Open a connection to the database using the settings in the environment."""
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        sslmode="require",
    )


def run(main, poll_interval: int, health_port: int, get_status) -> int:
    """Run main() once (poll_interval <= 0) or forever on a fixed interval."""
    if health_port:
        start_health_server(health_port, get_status)
    if poll_interval <= 0:
        return main()
    while True:
        try:
            main()
        except Exception as e:  # keep the service alive; the error is logged
            log_event(logging.ERROR, "unexpected_error", error=str(e))
        time.sleep(poll_interval)