# ADR-005: Compute and Deployment Strategy

**Status:** Accepted. Extends ADR-004.
**Date:** 2026-10-01

## Context

Day 4 added a FastAPI dashboard serving the `/site/{id}` API, an aggregated `/health` endpoint and a human-facing page. Unlike ingestion, it must be reachable from the internet, including by the grader. It reads the database and the bucket, loads the trained model, and must reach both ingestion containers to report their health. The question was whether this changes the compute decision of ADR-004.

## Decision

The dashboard runs as a third container on the same VM and the same private Docker network, `airbreda`. The workload is tiny: a page load makes a handful of requests, and a 60-second cache means the data sources are read at most once a minute. The VM has the capacity, the deployment path is already proven, and nothing new needs securing or paying for. A managed service such as Azure App Service or Container Apps would add HTTPS, zero-downtime deployments and scaling, none of which this assessment requires.

The container runs long-term with `docker run -d --restart unless-stopped`, like the ingestion containers, so all three services are managed one way. Docker starts at boot, so after a reboot all three return automatically; the ingestion containers passed a reboot test, and the dashboard uses the same policy. The image is built on the VM from Git, and redeploying is `git pull`, `docker build`, `docker rm -f` and `docker run`, with a few seconds of downtime.

The dashboard follows the same least-privilege approach. It connects as `airbreda_dashboard`, a database user that can only SELECT from `sensor_readings`, configured in its own `.env.dashboard`, and reads the bucket through the managed identity. Because it faces the internet, the container runs as a non-root user, error responses never reveal internal details, and only port 8000 is published; the ingestion health ports stay bound to `127.0.0.1`. Port 8000 is open to the developer's IP during development and to the grader at submission. The image installs exact versions exported from `uv.lock`, so the scikit-learn version that trained the model is the one that loads it.

Testing all three containers with Docker Compose on the laptop caught one dependency that exists only inside Docker: the aggregated `/health` reaches the ingestion containers by service name, so the dashboard run directly returned `"status": "degraded"`, while Compose returned `"status": "ok"`.

This extends ADR-004 rather than superseding it. The compute decision, one VM running long-lived containers with a restart policy, is unchanged; what changed is a third container, port 8000, a second restricted database user and a non-root container user.

## Consequences

The system still runs on one free VM with one deployment procedure. The dashboard shares the VM's single point of failure and resources, so a memory problem in one container can affect the others. Pages are served over plain HTTP, acceptable for public, non-personal open data, but production would need HTTPS. Deployment downtime of a few seconds fits within the ADR-003 error budget.

Moving the dashboard to a managed service would be justified if AirBreda needed HTTPS on a custom domain, availability beyond one VM, deployments without SSH access, or more traffic than one small VM can serve.
