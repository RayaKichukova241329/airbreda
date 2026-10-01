"""
dashboard.py - the AirBreda API and dashboard (FastAPI).

Routes
  GET /site/{site_id}  the latest real NO2, this site's latest traffic and the
                       model's prediction for the current traffic
  GET /health          both ingestion services' health, side by side
  GET /                the human-facing page, built on /site/{site_id} like any
                       other client of the API

Where each /site/{site_id} field comes from (required comment):
  site_id               the URL, checked against the four NDW sites
  no2_ug_m3             sensor_readings (PostgreSQL): the most recent non-null NO2
                        value for station NL10240, written by ingest_air.py
  intensity_veh_per_hr  the bucket: total_flow in this site's most recent
                        ndw/YYYY-MM-DD/HH-<site>.csv, written by ingest_traffic.py
  timestamp             that file's measurement time (when the traffic was measured)
  no2_exceedance_risk,
  no2_ug_m3_predicted   predict() in predict.py, which uses model.pkl baked into
                        this image, with the total intensity and the local hour
                        from features.py (the same functions used in training)
  total_intensity_veh_per_hr  the sum of all four sites' latest intensities
  no2_timestamp, no2_is_flagged  the same database row as no2_ug_m3

Why the prediction is the same for all four sites: the model was trained on the
TOTAL intensity at the interchange against one NO2 station (NL10240). Feeding it
a single site's intensity would be training-serving skew, because the model
would receive an input it was never trained on.

If predict() raises: the request does NOT fail. The route returns the real NO2
and traffic values with no2_ug_m3_predicted and no2_exceedance_risk set to null,
adds a prediction_error message and logs an ERROR. The real measurements are the
most trustworthy part of the response and remain useful when the model is
broken; failing the whole request would hide real data because of a model
problem. If the database or the bucket cannot be reached, the route returns 503:
then there is no real data to show, and returning anything else would mislead.
Error responses never include internal details (host names, error messages);
those go to the logs only.
"""

import io
import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

import predict as model
from common import get_connection, get_container_client, log_event, setup_logging
from features import hour_of_day

load_dotenv()
setup_logging()

STATION = "NL10240"
SITES = ("hrl", "hrr", "vwd", "vwa")
CACHE_SECONDS = 60  # the page makes four calls at once; read the data sources only once
HEALTHY_WITHIN = timedelta(hours=2)  # ingestion runs hourly, so an older fetch is overdue
AIR_HEALTH_URL = os.environ.get("AIR_HEALTH_URL", "http://air-ingest:8001/health")
TRAFFIC_HEALTH_URL = os.environ.get("TRAFFIC_HEALTH_URL", "http://traffic-ingest:8002/health")

app = FastAPI(title="AirBreda", version="1.0")
_cache: dict = {}


def iso_utc(ts) -> str:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert("UTC").isoformat().replace("+00:00", "Z")


def cached(key: str, loader):
    """Return a value loaded at most CACHE_SECONDS ago, or load it again."""
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = loader()
    _cache[key] = (now, value)
    return value


@lru_cache(maxsize=1)
def bucket():
    """One storage client for the lifetime of the app (managed identity on the VM)."""
    return get_container_client(source="dashboard")


def load_latest_no2() -> dict:
    """The most recent non-null NO2 reading for the station."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT timestamp, value, COALESCE(is_flagged, FALSE)
                FROM sensor_readings
                WHERE station_id = %s AND component = 'NO2' AND value IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (STATION,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        raise LookupError("no NO2 readings in the database")
    ts, value, flagged = row
    return {"no2_ug_m3": float(value), "no2_timestamp": iso_utc(ts), "no2_is_flagged": bool(flagged)}


def load_latest_traffic() -> dict:
    """Each site's most recent hourly file from the last two days of the bucket."""
    container = bucket()
    today = datetime.now(timezone.utc)
    names = []
    for day in (today, today - timedelta(days=1)):
        names += [b.name for b in container.list_blobs(name_starts_with=f"ndw/{day:%Y-%m-%d}/")]

    latest = {}
    for site in SITES:
        files = sorted(n for n in names if n.endswith(f"-{site}.csv"))
        if not files:
            continue
        row = pd.read_csv(io.BytesIO(container.download_blob(files[-1]).readall())).iloc[0]
        latest[site] = {"intensity_veh_per_hr": float(row["total_flow"]),
                        "timestamp": iso_utc(row["timestamp"])}
    return latest


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """One structured JSON log line per request, instead of uvicorn's plain text."""
    start = time.perf_counter()
    response = await call_next(request)
    log_event(logging.INFO if response.status_code < 500 else logging.ERROR, "http_request",
              method=request.method, path=request.url.path, status=response.status_code,
              duration_ms=round((time.perf_counter() - start) * 1000, 1))
    return response


@app.get("/site/{site_id}")
def site(site_id: str) -> dict:
    if site_id not in SITES:
        raise HTTPException(status_code=404,
                            detail=f"Unknown site. Use one of: {', '.join(SITES)}.")
    try:
        no2 = cached("no2", load_latest_no2)
        traffic = cached("traffic", load_latest_traffic)
    except Exception as e:
        log_event(logging.ERROR, "data_unavailable", site_id=site_id, error=str(e))
        raise HTTPException(status_code=503, detail="Real data is temporarily unavailable.")
    if site_id not in traffic:
        log_event(logging.ERROR, "data_unavailable", site_id=site_id,
                  error="no traffic file for this site in the last two days")
        raise HTTPException(status_code=503, detail="No recent traffic data for this site.")

    total = (sum(t["intensity_veh_per_hr"] for t in traffic.values())
             if len(traffic) == len(SITES) else None)
    response = {
        "site_id": site_id,
        "no2_ug_m3": no2["no2_ug_m3"],
        "intensity_veh_per_hr": traffic[site_id]["intensity_veh_per_hr"],
        "no2_exceedance_risk": None,
        "timestamp": traffic[site_id]["timestamp"],
        "no2_ug_m3_predicted": None,
        "total_intensity_veh_per_hr": total,
        "no2_timestamp": no2["no2_timestamp"],
        "no2_is_flagged": no2["no2_is_flagged"],
    }

    try:
        if total is None:
            raise ValueError("traffic is missing for at least one site, so the total is unknown")
        prediction = model.predict(total, hour_of_day(traffic[site_id]["timestamp"]))
        response["no2_ug_m3_predicted"] = prediction["no2_ug_m3_predicted"]
        response["no2_exceedance_risk"] = prediction["no2_exceedance_risk"]
    except Exception as e:
        log_event(logging.ERROR, "prediction_failed", site_id=site_id, error=str(e))
        response["prediction_error"] = "Prediction unavailable; the real values are still shown."
    return response


def fetch_health(url: str):
    """Read one ingestion container's own /health (Day 2) over the Docker network."""
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.load(r)
        return {"last_successful_fetch": data.get("last_successful_fetch"),
                "bad_data_count": data.get("bad_data_count")}, True
    except Exception as e:
        log_event(logging.WARNING, "health_check_failed", url=url, error=str(e))
        return {"last_successful_fetch": None, "bad_data_count": None}, False


def is_recent(ts) -> bool:
    return ts is not None and datetime.now(timezone.utc) - pd.Timestamp(ts) <= HEALTHY_WITHIN


@app.get("/health")
def health() -> dict:
    """"ok" when both ingestion services answer and fetched data within two hours."""
    air, air_ok = fetch_health(AIR_HEALTH_URL)
    ndw, ndw_ok = fetch_health(TRAFFIC_HEALTH_URL)
    healthy = (air_ok and ndw_ok and is_recent(air["last_successful_fetch"])
               and is_recent(ndw["last_successful_fetch"]))
    return {"status": "ok" if healthy else "degraded", "luchtmeetnet": air, "ndw": ndw}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AirBreda: NO2 and traffic at the A27/Breda interchange</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 52rem;
         padding: 0 1rem; color: #1f2933; }
  h1 { font-size: 1.5rem; margin-bottom: 0.25rem; }
  .sub { color: #52606d; margin-top: 0; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
           gap: 1rem; margin: 1.5rem 0; }
  .card { border: 1px solid #d9e2ec; border-radius: 8px; padding: 1rem; }
  .label { color: #52606d; font-size: 0.9rem; }
  .value { font-size: 1.8rem; font-weight: 600; margin: 0.25rem 0; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 0.5rem; border-bottom: 1px solid #d9e2ec; }
  .warn { color: #b44d12; }
  .muted { color: #52606d; font-size: 0.9rem; }
</style>
</head>
<body>
<h1>AirBreda</h1>
<p class="sub">NO&#8322; and traffic at the A27/Breda interchange
  (Luchtmeetnet station NL10240 and NDW measurement sites)</p>

<div class="cards">
  <div class="card"><div class="label">Actual NO&#8322; (measured)</div>
    <div class="value" id="actual">&ndash;</div><div class="muted" id="actual-time"></div></div>
  <div class="card"><div class="label">Predicted NO&#8322; (model)</div>
    <div class="value" id="predicted">&ndash;</div><div class="muted" id="risk"></div></div>
  <div class="card"><div class="label">Total traffic congestion</div>
    <div class="value" id="total">&ndash;</div><div class="muted">vehicles per hour, all four sites</div></div>
</div>

<table>
  <thead><tr><th>Site</th><th>Traffic (veh/h)</th><th>Predicted NO&#8322;</th>
    <th>Exceedance risk</th></tr></thead>
  <tbody id="sites"></tbody>
</table>

<p class="muted" id="updated"></p>
<p class="warn" id="warning"></p>

<script>
const SITES = ["hrl", "hrr", "vwd", "vwa"];
const STALE_MS = 3 * 60 * 60 * 1000;  // the same three hours the ingestion uses
const fmt = (v, unit) => (v === null || v === undefined) ? "n/a" : `${v}${unit}`;
const pct = v => (v === null || v === undefined) ? "n/a" : `${Math.round(v * 100)}%`;
const local = ts => new Date(ts).toLocaleString("nl-NL", { timeZone: "Europe/Amsterdam" });

function cell(row, text) {
  const td = document.createElement("td");
  td.textContent = text;  // textContent, never innerHTML: values are shown, not executed
  row.appendChild(td);
}

async function refresh() {
  const results = await Promise.all(SITES.map(async site => {
    try {
      const r = await fetch(`/site/${site}`);
      return r.ok ? await r.json() : { site_id: site, error: `HTTP ${r.status}` };
    } catch (e) {
      return { site_id: site, error: "unreachable" };
    }
  }));

  const ok = results.filter(r => !r.error);
  const first = ok[0];
  document.getElementById("actual").textContent = first ? fmt(first.no2_ug_m3, " \u00b5g/m\u00b3") : "n/a";
  document.getElementById("actual-time").textContent = first
    ? `measured for the hour ending ${local(first.no2_timestamp)}` + (first.no2_is_flagged ? " (flagged)" : "")
    : "";
  document.getElementById("predicted").textContent = first ? fmt(first.no2_ug_m3_predicted, " \u00b5g/m\u00b3") : "n/a";
  document.getElementById("risk").textContent = first ? `exceedance risk ${pct(first.no2_exceedance_risk)}` : "";
  const total = ok.length === SITES.length ? ok.reduce((s, r) => s + r.intensity_veh_per_hr, 0) : null;
  document.getElementById("total").textContent = total === null ? "n/a" : Math.round(total).toString();

  const body = document.getElementById("sites");
  body.replaceChildren();
  for (const r of results) {
    const row = document.createElement("tr");
    cell(row, r.site_id);
    if (r.error) { cell(row, r.error); cell(row, ""); cell(row, ""); }
    else {
      cell(row, Math.round(r.intensity_veh_per_hr).toString());
      cell(row, fmt(r.no2_ug_m3_predicted, " \u00b5g/m\u00b3"));
      cell(row, pct(r.no2_exceedance_risk));
    }
    body.appendChild(row);
  }

  const newest = ok.length ? Math.max(...ok.map(r => new Date(r.timestamp).getTime())) : null;
  document.getElementById("updated").textContent = newest
    ? `Traffic last measured ${local(newest)}. Page refreshes every 5 minutes.` : "";
  const stale = !newest || Date.now() - newest > STALE_MS
    || (first && Date.now() - new Date(first.no2_timestamp).getTime() > STALE_MS);
  document.getElementById("warning").textContent = stale
    ? "Warning: the data is more than three hours old. Ingestion may have stopped." : "";
}

refresh();
setInterval(refresh, 5 * 60 * 1000);
</script>
</body>
</html>
"""