"""
dashboard.py - the AirBreda API and dashboard (FastAPI).

Routes
  GET /site/{site_id}  the latest real NO2, this site's latest traffic and the
                       model's prediction for the current traffic
  GET /health          both ingestion services' health, side by side
  GET /history         hourly NO2 and total traffic for the last hours (default 48),
                       for the timeline on the page
  GET /model           what the model was trained on and how accurate it is,
                       read from model_metrics.json (baked in with model.pkl)
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
from pathlib import Path

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

METRICS_PATH = Path(__file__).with_name("model_metrics.json")

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


HISTORY_DEFAULT_HOURS = 48
HISTORY_MAX_HOURS = 168  # one week


def load_history_rows(hours: int):
    """NO2 rows and per-site hourly mean traffic flow from the database."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT timestamp, value, COALESCE(is_flagged, FALSE)
                FROM sensor_readings
                WHERE station_id = %s AND component = 'NO2'
                  AND timestamp > now() - make_interval(hours => %s)
                """,
                (STATION, hours + 1),
            )
            no2_rows = cur.fetchall()
            cur.execute(
                """
                SELECT date_trunc('hour', timestamp AT TIME ZONE 'UTC'), station_id, AVG(value)
                FROM sensor_readings
                WHERE component = 'flow' AND station_id = ANY(%s)
                  AND timestamp > now() - make_interval(hours => %s)
                GROUP BY 1, 2
                """,
                (list(SITES), hours),
            )
            flow_rows = cur.fetchall()
    finally:
        conn.close()
    return no2_rows, flow_rows


def build_history(no2_rows, flow_rows, end_hour: pd.Timestamp, hours: int) -> list:
    """One point per hour, keyed on the START of the hour (as in training).

    A Luchtmeetnet value stamped T is the average of T-1h to T, so it belongs to
    the hour starting at T-1h. Traffic is grouped by the hour it was measured in.
    An hour's total traffic is only given when all four sites reported; hours
    with missing data stay null, so gaps are visible instead of hidden.
    """
    no2 = {}
    for ts, value, flagged in no2_rows:
        start = pd.Timestamp(ts).tz_convert("UTC") - pd.Timedelta(hours=1)
        no2[start] = (None if value is None else float(value), bool(flagged))

    flows: dict = {}
    for hour, site_id, mean_flow in flow_rows:
        start = pd.Timestamp(hour)
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
        flows.setdefault(start, {})[site_id] = float(mean_flow)

    points = []
    for i in range(hours, -1, -1):
        start = end_hour - pd.Timedelta(hours=i)
        value, flagged = no2.get(start, (None, None))
        sites = flows.get(start, {})
        total = sum(sites.values()) if len(sites) == len(SITES) else None
        points.append({
            "hour_start": iso_utc(start),
            "no2_ug_m3": value,
            "no2_is_flagged": flagged,
            "total_intensity_veh_per_hr": total,
        })
    return points


@app.get("/history")
def history(hours: int = HISTORY_DEFAULT_HOURS) -> dict:
    """Hourly NO2 and total traffic for the timeline on the page."""
    if not 1 <= hours <= HISTORY_MAX_HOURS:
        raise HTTPException(status_code=422,
                            detail=f"hours must be between 1 and {HISTORY_MAX_HOURS}.")
    try:
        no2_rows, flow_rows = cached(f"history:{hours}", lambda: load_history_rows(hours))
    except Exception as e:
        log_event(logging.ERROR, "data_unavailable", route="/history", error=str(e))
        raise HTTPException(status_code=503, detail="History is temporarily unavailable.")
    end_hour = pd.Timestamp.now(tz="UTC").floor("h")
    return {"hours": hours, "points": build_history(no2_rows, flow_rows, end_hour, hours)}


@app.get("/model")
def model_info() -> dict:
    """The facts a reader needs to judge the prediction: data volume and error."""
    try:
        metrics = json.loads(METRICS_PATH.read_text())
        per_vehicle = metrics["coefficients"]["total_intensity_veh_per_hr"]
        return {
            "trained_at": metrics["trained_at"],
            "rows": metrics["rows"],
            "first_hour": metrics["first_hour"],
            "last_hour": metrics["last_hour"],
            "mae_leave_one_out": metrics["mae_leave_one_out"],
            "no2_change_per_1000_veh_per_hr": round(per_vehicle * 1000, 2),
            "exceedance_threshold_ug_m3": model.EXCEEDANCE_THRESHOLD_UG_M3,
        }
    except (OSError, ValueError, KeyError) as e:
        log_event(logging.ERROR, "model_info_unavailable", error=str(e))
        raise HTTPException(status_code=503, detail="Model information is unavailable.")


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
<title>AirBreda: air quality and traffic at the A27 Breda interchange</title>
<style>
  /* System fonts only: no third-party font service receives visitors' IP addresses. */
  :root {
    --ink: #1b2733;
    --muted: #5b6774;
    --paper: #f4f6f7;
    --panel: #ffffff;
    --line: #d5dce2;
    --road-blue: #1d4f91;
    --road-blue-soft: #c9d8ec;
    --shield-red: #c8102e;
    --low: #2e7d5b;
    --elevated: #a86512;
    --high: #b42318;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--paper); color: var(--ink);
    font: 400 1rem/1.5 "Segoe UI Variable Text", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    font-variant-numeric: tabular-nums;
  }
  main { max-width: 88rem; margin: 0 auto; padding: 1.5rem 1.5rem 2.5rem; }

  /* Header: identity on the left, freshness and the one action on the right */
  header { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 0.75rem 1.5rem; }
  .title { display: flex; align-items: center; gap: 0.85rem; }
  .shield {
    background: var(--shield-red); color: #fff; font-weight: 700; font-size: 1.05rem;
    padding: 0.2rem 0.55rem; border-radius: 4px; border: 2px solid #fff;
    box-shadow: 0 0 0 1.5px var(--shield-red); letter-spacing: 0.02em;
  }
  h1 { font-size: 1.3rem; line-height: 1.25; margin: 0; font-weight: 600; }
  .intro { color: var(--muted); margin: 0.15rem 0 0; max-width: 70ch; }
  .feeds { display: inline-flex; align-items: center; gap: 0.4rem; }
  .feeds::before { content: ""; width: 0.6rem; height: 0.6rem; border-radius: 50%; background: var(--muted); }
  .feeds.ok::before { background: var(--low); }
  .feeds.degraded::before { background: var(--high); }
  .about { margin: 1.25rem 0 0; padding-top: 0.9rem; border-top: 1px solid var(--line); color: var(--muted); font-size: 0.92rem; }
  .about strong { color: var(--ink); font-weight: 600; }
  .freshness { display: flex; align-items: center; gap: 1rem; color: var(--muted); }
  .freshness p { margin: 0; }
  button {
    font: inherit; color: var(--road-blue); background: var(--panel); border: 1.5px solid var(--road-blue);
    border-radius: 6px; padding: 0.4rem 0.85rem; cursor: pointer; white-space: nowrap;
  }
  button:hover { background: #eaf0f8; }
  button:focus-visible { outline: 3px solid var(--road-blue); outline-offset: 2px; }
  button:disabled { opacity: 0.6; cursor: progress; }

  .alert {
    margin: 1rem 0 0; padding: 0.7rem 1rem; border-left: 4px solid var(--high);
    background: #fdecea; color: #7a1b12; border-radius: 0 4px 4px 0;
  }
  .alert[hidden] { display: none; }

  /* Wide grid: air on the left, traffic on the right, the timeline across the bottom */
  .grid {
    display: grid; gap: 1.25rem; margin-top: 1.25rem;
    grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr);
    grid-template-areas: "air traffic" "timeline timeline";
  }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 1.25rem 1.5rem; }
  .air { grid-area: air; }
  .traffic { grid-area: traffic; }
  .timeline { grid-area: timeline; }
  h2 { font-size: 1rem; font-weight: 600; margin: 0 0 0.75rem; color: var(--muted); }

  .status { font-size: 1.55rem; line-height: 1.3; font-weight: 600; margin: 0 0 1.5rem; max-width: 36ch; }
  .level { padding: 0 0.35rem; border-radius: 4px; color: #fff; }
  .level.low { background: var(--low); }
  .level.elevated { background: var(--elevated); }
  .level.high { background: var(--high); }

  .scale { position: relative; height: 0.9rem; display: flex; margin: 0 0.6rem; }
  .zone { height: 100%; }
  .zone.low { background: #cfe6db; border-radius: 999px 0 0 999px; }
  .zone.elevated { background: #f3dfbf; }
  .zone.high { background: #f4cdc9; border-radius: 0 999px 999px 0; }
  .tick { position: absolute; top: -0.35rem; bottom: -0.35rem; width: 2px; background: var(--ink); }
  .tick-label { position: absolute; top: 1.4rem; transform: translateX(-50%); font-size: 0.8rem; color: var(--muted); white-space: nowrap; }
  .marker { position: absolute; top: 50%; transform: translate(-50%, -50%); border-radius: 50%; transition: left 0.6s ease; }
  .marker.measured { width: 1.25rem; height: 1.25rem; background: var(--ink); border: 3px solid #fff; box-shadow: 0 0 0 1.5px var(--ink); }
  .marker.predicted { width: 1.1rem; height: 1.1rem; background: #fff; border: 3px dashed var(--road-blue); }
  .marker[hidden] { display: none; }
  .scale-ends { display: flex; justify-content: space-between; color: var(--muted); font-size: 0.8rem; margin: 2.4rem 0.6rem 0; }

  .readings { display: grid; grid-template-columns: 1fr 1fr; gap: 1.25rem; margin-top: 1.25rem; }
  .reading { border-top: 3px solid var(--ink); padding-top: 0.55rem; }
  .reading.predicted { border-top: 3px dashed var(--road-blue); }
  .reading .value { font-size: 1.45rem; font-weight: 600; margin: 0.1rem 0; }
  .reading p { margin: 0; color: var(--muted); font-size: 0.92rem; }

  .total { font-size: 1.55rem; font-weight: 600; margin: 0 0 1rem; }
  .total span { font-size: 1rem; font-weight: 400; color: var(--muted); }
  .sites { list-style: none; margin: 0; padding: 0; }
  .site { display: grid; grid-template-columns: 1fr 6rem; gap: 0.2rem 1rem; padding: 0.6rem 0; border-bottom: 1px solid var(--line); }
  .site:last-child { border-bottom: 0; }
  .site-name { font-weight: 500; }
  .site-code { color: var(--muted); font-size: 0.82rem; }
  .site-flow { text-align: right; font-weight: 600; align-self: center; }
  .bar { grid-column: 1 / -1; height: 0.45rem; background: #e3e8ec; border-radius: 999px; overflow: hidden; }
  .bar span { display: block; height: 100%; width: 0; background: var(--road-blue); border-radius: 999px; transition: width 0.6s ease; }
  .site-pred { grid-column: 1 / -1; color: var(--muted); font-size: 0.82rem; }

  .timeline-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; gap: 0.5rem 1.5rem; }
  .legend { display: flex; flex-wrap: wrap; gap: 0.4rem 1.25rem; color: var(--muted); font-size: 0.85rem; margin: 0; padding: 0; list-style: none; }
  .legend i { display: inline-block; vertical-align: middle; margin-right: 0.4rem; }
  .key-no2 { width: 1.4rem; height: 3px; background: var(--ink); }
  .key-traffic { width: 0.8rem; height: 0.8rem; background: var(--road-blue-soft); border: 1px solid var(--road-blue); }
  .key-limit { width: 1.4rem; border-top: 2px dashed var(--high); }
  .summary { color: var(--muted); margin: 0.25rem 0 0.75rem; }
  .chart { position: relative; }
  .chart svg { display: block; width: 100%; height: auto; }
  .tooltip {
    position: absolute; pointer-events: none; background: var(--ink); color: #fff; font-size: 0.85rem;
    padding: 0.45rem 0.65rem; border-radius: 6px; white-space: nowrap; transform: translate(-50%, -110%);
  }
  .tooltip[hidden] { display: none; }

  footer { margin-top: 1.5rem; color: var(--muted); font-size: 0.85rem; display: grid; grid-template-columns: repeat(auto-fit, minmax(20rem, 1fr)); gap: 0.5rem 2rem; }
  footer p { margin: 0; }

  @media (max-width: 62rem) {
    .grid { grid-template-columns: 1fr; grid-template-areas: "air" "traffic" "timeline"; }
  }
  @media (max-width: 30rem) {
    main { padding: 1rem; }
    .status { font-size: 1.25rem; }
    .readings { grid-template-columns: 1fr; }
    .panel { padding: 1rem; }
  }
  @media (prefers-reduced-motion: reduce) { .marker, .bar span { transition: none; } }
</style>
</head>
<body>
<main>
  <header>
    <div class="title">
      <span class="shield" aria-hidden="true">A27</span>
      <div>
        <h1>Air quality and traffic at the Breda interchange</h1>
        <p class="intro">Does traffic at the A27 interchange push air pollution nearby above safe levels?
          This page compares measured NO₂ with live traffic and with a model's prediction.</p>
      </div>
    </div>
    <div class="freshness">
      <p class="feeds" id="feeds">Checking data feeds&hellip;</p>
      <p id="updated" aria-live="polite">Loading&hellip;</p>
      <button type="button" id="refresh">Refresh now</button>
    </div>
  </header>
  <p class="alert" id="alert" role="alert" hidden></p>

  <div class="grid">
    <section class="panel air" aria-labelledby="air-title">
      <h2 id="air-title">Air right now</h2>
      <p class="status" id="status">Loading the latest readings&hellip;</p>
      <div class="scale" aria-hidden="true">
        <div class="zone low" id="zone-low"></div>
        <div class="zone elevated" id="zone-elevated"></div>
        <div class="zone high" id="zone-high"></div>
        <div class="tick" id="tick-who"></div><span class="tick-label" id="label-who">WHO guideline 25</span>
        <div class="tick" id="tick-eu"></div><span class="tick-label" id="label-eu">EU limit 40</span>
        <div class="marker measured" id="marker-measured" hidden></div>
        <div class="marker predicted" id="marker-predicted" hidden></div>
      </div>
      <div class="scale-ends" aria-hidden="true"><span>0 µg/m³</span><span id="scale-max"></span></div>
      <div class="readings">
        <div class="reading">
          <p>Measured NO₂</p>
          <div class="value" id="measured">&ndash;</div>
          <p id="measured-note"></p>
        </div>
        <div class="reading predicted">
          <p>Predicted for this hour</p>
          <div class="value" id="predicted">&ndash;</div>
          <p id="risk"></p>
          <p id="predicted-note"></p>
        </div>
      </div>
      <p class="about" id="about"></p>
    </section>

    <section class="panel traffic" aria-labelledby="traffic-title">
      <h2 id="traffic-title">Traffic right now</h2>
      <p class="total" id="total">&ndash;</p>
      <ul class="sites" id="sites"></ul>
    </section>

    <section class="panel timeline" aria-labelledby="timeline-title">
      <div class="timeline-head">
        <h2 id="timeline-title">The last 48 hours</h2>
        <ul class="legend">
          <li><i class="key-no2"></i>Measured NO₂</li>
          <li><i class="key-traffic"></i>Total traffic</li>
          <li><i class="key-limit"></i>EU limit, 40 µg/m³</li>
        </ul>
      </div>
      <p class="summary" id="summary"></p>
      <div class="chart" id="chart">
        <svg id="chart-svg" viewBox="0 0 1000 296" role="img" aria-labelledby="summary"></svg>
        <div class="tooltip" id="tooltip" hidden></div>
      </div>
    </section>
  </div>

  <footer>
    <p>Measured NO₂ comes from RIVM's Luchtmeetnet station Breda-Tilburgseweg (NL10240), as an average over one hour.
      Traffic comes from NDW open data for four measurement points at the A27 interchange.</p>
    <p>The prediction comes from a linear regression on total traffic and time of day, trained on the hours this system has collected so far, so treat it as indicative.
      The EU limit of 40 µg/m³ is an annual average: a single hour above it is not a legal breach, but it does mean the air is poor.
      The WHO guideline of 25 µg/m³ applies to a 24-hour average. The page refreshes every 5 minutes.</p>
  </footer>
</main>

<script>
const SITES = [
  { id: "hrl", name: "A27 main road, direction 1" },
  { id: "hrr", name: "A27 main road, direction 2" },
  { id: "vwd", name: "Slip road onto the A27, leaving Breda" },
  { id: "vwa", name: "Slip road off the A27, into Breda" },
];
const WHO = 25, EU = 40;
const STALE_MS = 3 * 60 * 60 * 1000;   // the same three hours the ingestion uses
const REFRESH_MS = 5 * 60 * 1000;
const SVG_NS = "http://www.w3.org/2000/svg";
const $ = id => document.getElementById(id);

const num = v => v.toLocaleString("en-GB", { maximumFractionDigits: 2 });  // same precision as /site/{id}
const ug = v => `${num(v)} µg/m³`;
const veh = v => `${Math.round(v).toLocaleString("en-GB")} veh/h`;
const tz = { timeZone: "Europe/Amsterdam" };
const when = ts => new Date(ts).toLocaleString("en-GB", { ...tz, day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const clock = ts => new Date(ts).toLocaleTimeString("en-GB", { ...tz, hour: "2-digit", minute: "2-digit" });
const dayHour = ts => new Date(ts).toLocaleString("en-GB", { ...tz, weekday: "short", hour: "2-digit", minute: "2-digit" });

function level(no2) {
  if (no2 > EU) return { cls: "high", word: "high", detail: `above the EU limit of ${EU}` };
  if (no2 > WHO) return { cls: "elevated", word: "elevated", detail: `above the WHO guideline of ${WHO}, below the EU limit of ${EU}` };
  return { cls: "low", word: "low", detail: `well below the EU limit of ${EU}` };
}
const riskWord = r => r >= 0.5 ? "likely" : r >= 0.2 ? "possible" : "unlikely";

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  Object.assign(node, attrs);
  if (text !== undefined) node.textContent = text;   // textContent, never innerHTML: data is shown, not executed
  return node;
}
function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}


const FEED_STALE_MS = 2 * 60 * 60 * 1000;   // ingestion runs hourly, so an older fetch is overdue

function drawFeeds(health) {
  const feeds = $("feeds");
  feeds.className = "feeds";
  if (health.error) { feeds.textContent = "Data feed status unknown"; return; }
  const late = [["Luchtmeetnet", health.luchtmeetnet], ["NDW", health.ndw]]
    .filter(([, f]) => !f.last_successful_fetch || Date.now() - new Date(f.last_successful_fetch).getTime() > FEED_STALE_MS)
    .map(([name]) => name);
  feeds.classList.add(late.length ? "degraded" : "ok");
  feeds.textContent = late.length ? `${late.join(" and ")} feed delayed` : "Both data feeds running";
}

function drawAbout(info) {
  const about = $("about");
  if (info.error) { about.textContent = "Details about the model aren't available right now."; return; }
  const change = info.no2_change_per_1000_veh_per_hr;
  const direction = change >= 0 ? "more" : "less";
  const day = ts => new Date(ts).toLocaleDateString("en-GB", { ...tz, day: "numeric", month: "short" });
  about.replaceChildren(
    el("strong", {}, "What the data shows so far. "),
    `In the ${info.rows} hours collected (${day(info.first_hour)} to ${day(info.last_hour)}), each extra 1,000 vehicles per hour goes with about `
      + `${num(Math.abs(change))} µg/m³ ${direction} NO₂. On hours it hadn't seen, the model is off by about ${num(info.mae_leave_one_out)} µg/m³ on average.`
      + (info.rows < 24 ? " That's too few hours to draw conclusions yet: the model is retrained as more data comes in." : ""));
}

function drawScale(measured, predicted) {
  const max = Math.max(60, Math.ceil((Math.max(measured ?? 0, predicted ?? 0) + 5) / 10) * 10);
  const pos = v => `${Math.min(v, max) / max * 100}%`;
  $("zone-low").style.width = pos(WHO);
  $("zone-elevated").style.width = `${(EU - WHO) / max * 100}%`;
  $("zone-high").style.width = `${(max - EU) / max * 100}%`;
  for (const [tick, label, v] of [["tick-who", "label-who", WHO], ["tick-eu", "label-eu", EU]]) {
    $(tick).style.left = pos(v);
    $(label).style.left = pos(v);
  }
  $("scale-max").textContent = `${max} µg/m³`;
  for (const [id, v] of [["marker-measured", measured], ["marker-predicted", predicted]]) {
    const m = $(id);
    m.hidden = v === null || v === undefined;
    if (!m.hidden) m.style.left = pos(v);
  }
}

function drawAir(first) {
  const status = $("status");
  if (!first) {
    status.textContent = "Live readings aren't available right now.";
    ["measured", "predicted"].forEach(id => $(id).textContent = "–");
    ["measured-note", "risk", "predicted-note"].forEach(id => $(id).textContent = "");
    drawScale(null, null);
    return;
  }
  const lvl = level(first.no2_ug_m3);
  status.replaceChildren("NO₂ is ", el("span", { className: `level ${lvl.cls}` }, lvl.word),
                         `: ${ug(first.no2_ug_m3)}, ${lvl.detail}.`);
  const hourStart = new Date(first.no2_timestamp).getTime() - 3600 * 1000;
  $("measured").textContent = ug(first.no2_ug_m3);
  $("measured-note").textContent = `Average for ${clock(hourStart)}–${clock(first.no2_timestamp)}`
    + (first.no2_is_flagged ? " (flagged as unreliable)" : "");
  const hasPrediction = first.no2_ug_m3_predicted !== null;
  $("predicted").textContent = hasPrediction ? ug(first.no2_ug_m3_predicted) : "Unavailable";
  $("risk").textContent = hasPrediction
    ? `Exceeding ${EU} µg/m³ is ${riskWord(first.no2_exceedance_risk)} (${Math.round(first.no2_exceedance_risk * 100)}% risk)`
    : "The model couldn't make a prediction. Measured values are still current.";
  $("predicted-note").textContent = hasPrediction ? `From traffic measured at ${clock(first.timestamp)} and the time of day` : "";
  drawScale(first.no2_ug_m3, hasPrediction ? first.no2_ug_m3_predicted : null);
}

function drawTraffic(results, ok) {
  const total = ok.length === SITES.length ? ok.reduce((s, r) => s + r.intensity_veh_per_hr, 0) : null;
  $("total").replaceChildren(...(total === null
    ? ["Incomplete: not every site reported"]
    : [Math.round(total).toLocaleString("en-GB"), el("span", {}, " vehicles per hour, all four sites together")]));

  const max = Math.max(1, ...ok.map(r => r.intensity_veh_per_hr));
  const list = $("sites");
  list.replaceChildren();
  for (const site of SITES) {
    const r = results.find(x => x.site_id === site.id);
    const fill = el("span");
    const flow = el("div", { className: "site-flow" });
    const pred = el("div", { className: "site-pred" });
    if (!r || r.error) {
      flow.textContent = "No data";
      pred.textContent = "This site's latest traffic reading isn't available.";
    } else {
      flow.textContent = veh(r.intensity_veh_per_hr);
      fill.style.width = `${r.intensity_veh_per_hr / max * 100}%`;
      pred.textContent = r.no2_ug_m3_predicted === null ? "Prediction unavailable"
        : `Predicted NO₂ ${ug(r.no2_ug_m3_predicted)}, exceedance risk ${Math.round(r.no2_exceedance_risk * 100)}%`;
    }
    const label = el("div");
    label.append(el("div", { className: "site-name" }, site.name), el("div", { className: "site-code" }, `NDW site ${site.id}`));
    const bar = el("div", { className: "bar" });
    bar.append(fill);
    const li = el("li", { className: "site" });
    li.append(label, flow, bar, pred);
    list.append(li);
  }
}

function drawTimeline(points) {
  const chart = $("chart-svg");
  chart.replaceChildren();
  const tooltip = $("tooltip");
  const W = 1000, H = 296, L = 48, R = 64, T = 34, B = 34;  // T leaves room for the unit labels above the axes
  const n = points.length;
  if (!n) { $("summary").textContent = "No history available yet."; return; }

  const no2Values = points.map(p => p.no2_ug_m3).filter(v => v !== null);
  const traffic = points.map(p => p.total_intensity_veh_per_hr).filter(v => v !== null);
  const no2Max = Math.max(50, Math.ceil((Math.max(0, ...no2Values) + 5) / 10) * 10);
  const trafficMax = Math.max(1000, Math.ceil(Math.max(0, ...traffic) / 1000) * 1000);
  const step = (W - L - R) / n;
  const x = i => L + i * step;
  const yNo2 = v => T + (H - T - B) * (1 - v / no2Max);
  const yTraffic = v => T + (H - T - B) * (1 - v / trafficMax);

  // grid lines and axis labels
  for (let v = 0; v <= no2Max; v += 10) {
    chart.append(svg("line", { x1: L, x2: W - R, y1: yNo2(v), y2: yNo2(v), stroke: "#e3e8ec" }));
    const t = svg("text", { x: L - 8, y: yNo2(v) + 4, "text-anchor": "end", "font-size": 12, fill: "#5b6774" });
    t.textContent = v;
    chart.append(t);
  }
  for (const v of [0, trafficMax / 2, trafficMax]) {
    const t = svg("text", { x: W - R + 8, y: yTraffic(v) + 4, "font-size": 12, fill: "#1d4f91" });
    t.textContent = v.toLocaleString("en-GB");
    chart.append(t);
  }
  const unitL = svg("text", { x: L - 8, y: T - 16, "text-anchor": "end", "font-size": 11, fill: "#5b6774" });
  unitL.textContent = "µg/m³";
  const unitR = svg("text", { x: W - R + 8, y: T - 16, "font-size": 11, fill: "#1d4f91" });
  unitR.textContent = "veh/h";
  chart.append(unitL, unitR);

  // traffic bars (right axis)
  points.forEach((p, i) => {
    if (p.total_intensity_veh_per_hr === null) return;
    const y = yTraffic(p.total_intensity_veh_per_hr);
    chart.append(svg("rect", { x: x(i) + step * 0.15, y, width: step * 0.7, height: H - B - y,
      fill: "#c9d8ec", stroke: "#1d4f91", "stroke-width": 0.6 }));
  });

  // EU limit line (left axis)
  chart.append(svg("line", { x1: L, x2: W - R, y1: yNo2(EU), y2: yNo2(EU), stroke: "#b42318", "stroke-width": 1.5, "stroke-dasharray": "6 5" }));

  // NO2 line, broken where hours are missing
  let segment = [];
  const flush = () => {
    if (segment.length > 1) chart.append(svg("polyline", { points: segment.join(" "), fill: "none", stroke: "#1b2733", "stroke-width": 2.25, "stroke-linejoin": "round" }));
    segment = [];
  };
  points.forEach((p, i) => {
    if (p.no2_ug_m3 === null) { flush(); return; }
    segment.push(`${x(i) + step / 2},${yNo2(p.no2_ug_m3)}`);
  });
  flush();
  points.forEach((p, i) => {
    if (p.no2_ug_m3 !== null && p.no2_is_flagged)
      chart.append(svg("circle", { cx: x(i) + step / 2, cy: yNo2(p.no2_ug_m3), r: 4, fill: "#fff", stroke: "#a86512", "stroke-width": 2 }));
  });

  // time labels every 6 hours, local time
  points.forEach((p, i) => {
    const hour = Number(new Date(p.hour_start).toLocaleString("en-GB", { ...tz, hour: "2-digit", hourCycle: "h23" }));
    if (hour % 6 !== 0) return;
    chart.append(svg("line", { x1: x(i), x2: x(i), y1: H - B, y2: H - B + 5, stroke: "#5b6774" }));
    const t = svg("text", { x: x(i), y: H - B + 20, "text-anchor": "middle", "font-size": 12, fill: "#5b6774" });
    t.textContent = hour === 0 ? new Date(p.hour_start).toLocaleDateString("en-GB", { ...tz, weekday: "short", day: "numeric" }) : `${String(hour).padStart(2, "0")}:00`;
    chart.append(t);
  });
  chart.append(svg("line", { x1: L, x2: W - R, y1: H - B, y2: H - B, stroke: "#5b6774" }));

  // hover areas: one invisible column per hour, with a tooltip
  points.forEach((p, i) => {
    const area = svg("rect", { x: x(i), y: T, width: step, height: H - T - B, fill: "transparent" });
    const text = `${dayHour(p.hour_start)}: NO₂ ${p.no2_ug_m3 === null ? "no data" : ug(p.no2_ug_m3)}, traffic ${p.total_intensity_veh_per_hr === null ? "no data" : veh(p.total_intensity_veh_per_hr)}`;
    area.addEventListener("mouseenter", () => {
      const box = chart.getBoundingClientRect();
      tooltip.textContent = text;
      tooltip.style.left = `${(x(i) + step / 2) / W * box.width}px`;
      tooltip.style.top = `${T / H * box.height + 8}px`;
      tooltip.hidden = false;
    });
    area.addEventListener("mouseleave", () => { tooltip.hidden = true; });
    const title = svg("title");
    title.textContent = text;
    area.append(title);
    chart.append(area);
  });

  const withTraffic = traffic.length;
  $("summary").textContent = no2Values.length
    ? `NO₂ ranged from ${ug(Math.min(...no2Values))} to ${ug(Math.max(...no2Values))}. `
      + `Traffic is available for ${withTraffic} of ${n} hours, from the moment this system started collecting it.`
    : "No measured NO₂ in this period yet.";
}

async function getJson(url) {
  try {
    const r = await fetch(url);
    return r.ok ? await r.json() : { error: r.status };
  } catch (e) {
    return { error: "unreachable" };
  }
}

async function refresh() {
  const button = $("refresh");
  button.disabled = true;
  button.textContent = "Refreshing…";
  const [results, hist, health, info] = await Promise.all([
    Promise.all(SITES.map(async s => ({ site_id: s.id, ...(await getJson(`/site/${s.id}`)) }))),
    getJson("/history?hours=48"),
    getJson("/health"),
    getJson("/model"),
  ]);
  const ok = results.filter(r => !r.error);
  const first = ok[0];

  drawAir(first);
  drawAbout(info);
  drawFeeds(health);
  drawTraffic(results, ok);
  if (hist.error) $("summary").textContent = "The 48-hour history isn't available right now.";
  else drawTimeline(hist.points);

  const alert = $("alert");
  alert.hidden = true;
  if (!first) {
    $("updated").textContent = `No live data. Trying again at ${clock(Date.now() + REFRESH_MS)}.`;
  } else {
    const newest = Math.max(...ok.map(r => new Date(r.timestamp).getTime()));
    $("updated").textContent = `Traffic measured ${when(newest)}. Next refresh ${clock(Date.now() + REFRESH_MS)}.`;
    if (Date.now() - newest > STALE_MS || Date.now() - new Date(first.no2_timestamp).getTime() > STALE_MS) {
      alert.textContent = "These readings are more than three hours old. Data collection may have stopped, so the numbers may not reflect the air right now.";
      alert.hidden = false;
    }
  }
  button.disabled = false;
  button.textContent = "Refresh now";
}

$("refresh").addEventListener("click", refresh);
refresh();
setInterval(refresh, REFRESH_MS);
</script>
</body>
</html>
"""