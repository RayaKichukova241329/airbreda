# ADR-003: Resilience Strategy

**Status:** Accepted
**Date:** 2026-10-01

## Context

The `/site/{id}` endpoint and the dashboard serve residents and policy staff; nothing safety-critical depends on them, and the data changes hourly. Downtime has two costs, though. An unavailable dashboard is an inconvenience. Unavailable ingestion is worse for one source: Luchtmeetnet backfills its last 50 hours after recovery, but NDW only exposes current traffic, so every hour of downtime loses that hour of traffic and training data permanently.

The deployment has no redundancy: one VM (Poland Central), one database and one storage account (Italy North). Azure's published SLAs for a single VM with Premium SSD, a Flexible Server without high availability and LRS blob storage are each 99.9%, so together they allow about 99.7% before deployments and operator mistakes are counted, with no one on call. The December 2021 AWS outage in us-east-1 (about five hours, by the course's figure) showed what a regional failure looks like: workloads that were already running mostly continued, but many could not be launched, restarted or changed.

## Decision

The SLO for `/site/{id}` is set at **99.5%** per month: an error budget of 216 minutes per 30-day month, or 43.8 hours per year. A 99.9% target (43 minutes per month) exceeds what the underlying services promise without redundancy; a single five-hour outage would breach it sevenfold. A 99% target (7.2 hours per month) would hide real problems. At 99.5%, a five-hour outage uses 139% of one month's budget and 11.4% of the year's: a breach for that month, recoverable over the year, which fits an hourly air-quality dashboard.

The chosen DR tier is **Backup and Restore**. The database takes automatic backups with point-in-time restore over seven days, included in the free offer. Everything else is reproducible: code and model are in Git, images are rebuilt from the repository, and secrets are re-created by the operator. Recovery means a new VM, Docker, a clone, the two environment files and three containers, plus a database restore if needed: an estimated recovery time of one to two hours. The database can be restored to a point close to the moment of failure, NO₂ is backfilled automatically if the outage lasts under 50 hours, but NDW traffic is lost for the full outage duration. The backups are stored in the same region as the server, so a complete regional outage would mean waiting for Italy North to recover; geo-redundant backups would cover that case, but they can only be chosen when a server is created.

Within this tier, resilience comes from the application: the air quality ingestion retries with exponential backoff (2, 4, 8 seconds), the pattern whose absence amplified the 2021 outage; all containers run with `--restart unless-stopped` and Docker starts at boot, which reboot tests verified for all three containers; idempotent writes, so any run can be repeated; and structured logs, `/health` endpoints and a stale-feed warning after three hours.

## Consequences

Backup and Restore costs nothing extra and suits one developer without an on-call rota. The price is a manual recovery measured in hours and permanent gaps in traffic history while ingestion is down; a five-hour gap shrinks the training set but does not invalidate it.

The next tier, Pilot Light, would keep a live copy of the data in a second region. On Azure, read replicas, like high availability, are not supported on the Burstable tier, so it would require moving the primary to General Purpose and running a same-size replica in a second region, both outside the free offer, plus geo-redundant storage. At €143.89 per month for each General Purpose D2ds v5 server (Azure pricing calculator, October 2026), that is about €288 per month more, assuming the same price in the second region, against a current database cost of zero, and it would still lose traffic during failover.

Moving up a tier would be justified if AirBreda issued time-critical public alerts, or if traffic history became too valuable to lose. A cheaper first step would be a small ingestion-only VM in a second region saving traffic files there. The `/health` counters live in memory and reset on restart.
