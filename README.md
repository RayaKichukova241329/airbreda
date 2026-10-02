# AirBreda

AirBreda investigates whether traffic congestion at the A27 interchange near Breda pushes air pollution nearby above safe levels. It ingests hourly NO₂ averages from Luchtmeetnet station NL10240 and one-minute traffic counts from four NDW measurement sites at the interchange, stores them in Azure, trains a regression model on the collected data and serves predictions through an API and a dashboard.

The full architecture, including the six architecture decision records, is published as the [Architecture Design Document](https://rayakichukova241329.github.io/airbreda/) on GitHub Pages (source in [`docs/`](docs/)).

## How it works

Three containers run on one Azure VM, connected by a private Docker network:

| Container | Image | What it does |
|---|---|---|
| `air-ingest` | `Dockerfile` | Every hour: fetches NO₂ from Luchtmeetnet, flags null and stale readings, writes them to PostgreSQL |
| `traffic-ingest` | `Dockerfile.traffic` | Every hour: downloads the national NDW feed, extracts the four sites, uploads one CSV per site to Blob Storage and writes valid readings to PostgreSQL |
| `dashboard` | `Dockerfile.dashboard` | Serves the API and the dashboard page on port 8000, with the trained model baked into the image |

Data is stored in Azure Database for PostgreSQL (table `sensor_readings`) and Azure Blob Storage (container `airbreda-raw`). Both ingestion services log structured JSON and expose their own `/health` on ports 8001 and 8002, reachable inside the VM only.

## Measurement sites

All four sites are real NDW measurement points at the same A27 interchange as Luchtmeetnet station NL10240.

| Site ID | NDW measurement site | Location |
|---|---|---|
| `hrl` | `RWS01_MONIBAS_0271hrl0063ra` | A27 main carriageway, direction 1 |
| `hrr` | `RWS01_MONIBAS_0271hrr0063ra` | A27 main carriageway, direction 2 |
| `vwd` | `RWS01_MONIBAS_0270vwd0063ra` | Slip road onto the A27, leaving Breda |
| `vwa` | `RWS01_MONIBAS_0270vwa0063ra` | Slip road off the A27, into Breda |

## API

| Endpoint | Returns |
|---|---|
| `GET /site/{site_id}` | Latest measured NO₂, the site's latest traffic intensity, and the model's predicted NO₂ and exceedance risk |
| `GET /health` | Status of both ingestion services: last successful fetch and bad-data count per source |
| `GET /history?hours=48` | Hourly NO₂, total traffic and the current model's prediction for the last hours, used by the timeline on the page |
| `GET /model` | What the model was trained on and how accurate it is |
| `GET /` | The dashboard page |

Example: `GET /site/hrl`

```json
{"site_id": "hrl", "no2_ug_m3": 15.45, "intensity_veh_per_hr": 1500.0,
 "no2_exceedance_risk": 0.008, "timestamp": "2026-10-01T16:59:00Z", "...": "..."}
```

## Data quality

| Source | Problem | Handling |
|---|---|---|
| Luchtmeetnet | Null value, or the same value for 3 or more consecutive hours | Row is kept with `is_flagged = TRUE` and a `DATA_QUALITY_ERROR` warning is logged; flagged rows are excluded from training |
| NDW | `speed = -1` (no valid speed for a lane) | The speed row is not written to the database; the flow row and the bucket file are kept; a warning is logged |

More than 10 data quality events from one source within an hour log a single `BAD_DATA_THRESHOLD_EXCEEDED` error.

## Repository structure

| Path | Purpose |
|---|---|
| `ingest_air.py`, `ingest_traffic.py` | The two ingestion services |
| `getTrafficReadings.py` | NDW parsing helpers, adapted from the BUas course material |
| `common.py` | Shared logging, bad-data counter, `/health` server, database and storage connections |
| `features.py` | Feature definitions shared by training and serving |
| `build_training_data.py`, `train_model.py` | Build `training_data.csv` and train `model.pkl` |
| `predict.py` | Prediction and exceedance risk, used by the dashboard |
| `dashboard.py` | The FastAPI app |
| `scripts/` | One-off setup: create the table and the two restricted database users |
| `tests/` | Unit tests; they use fake data sources and need no cloud access |
| `docker-compose.yml` | Local test of all three containers together |
| `docs/` | The Architecture Design Document (GitHub Pages) |

## Running it

**Requirements:** Python 3.12 with [uv](https://docs.astral.sh/uv/), Docker, and an Azure subscription with a PostgreSQL Flexible Server and a storage account.

```bash
uv sync                       # install the exact dependency versions from uv.lock
cp .env.example .env          # then fill in the values; .env is never committed
uv run python -m pytest tests/
```

**One-time setup** (with the admin credentials in `.env`):

```bash
uv run python -m scripts.create_table
uv run python -m scripts.create_ingest_user
uv run python -m scripts.create_dashboard_user
```

**Local test of the full stack:**

```bash
docker compose up --build     # dashboard on http://localhost:8000
```

**Deployment on the VM:** the images are built from this repository and run with `docker run -d --restart unless-stopped` on a Docker network called `airbreda`. Ingestion containers get `POLL_INTERVAL_SECONDS=3600` and their `HEALTH_PORT`; on the VM, storage is accessed through the VM's managed identity, so `AZURE_STORAGE_CONNECTION_STRING` stays empty and `AZURE_STORAGE_ACCOUNT_URL` is set instead. The dashboard uses its own `.env.dashboard` with the read-only database user.

## Retraining the model

```bash
uv run python build_training_data.py   # joins NO₂ (database) and traffic (bucket)
uv run python train_model.py           # writes model.pkl and model_metrics.json
uv run python -m pytest tests/
```

Commit `model.pkl`, `model_metrics.json` and `training_data.csv`, then on the VM pull, rebuild the dashboard image and restart the dashboard container. The model is baked into the image, so it only changes on redeployment.

## Notes

- Both ingestion services can also publish readings to a Redis queue (Day 2). This is switched off in the current deployment: it only runs when `REDIS_HOST` is set (see ADR-002 and ADR-004).
- No credentials are stored in this repository. Secrets live only in `.env` files, created separately on each machine.

## Data sources

Air quality data: [Luchtmeetnet](https://www.luchtmeetnet.nl/) (RIVM), station NL10240 Breda-Tilburgseweg. Traffic data: [NDW](https://www.ndw.nu/) open data. Both are public open data, used without an API key.
