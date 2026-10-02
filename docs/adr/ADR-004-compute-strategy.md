# ADR-004: Compute Strategy

**Status:** Accepted. Extended by ADR-005.
**Date:** 2026-10-01

## Context

Until Day 3, ingestion ran on the developer's laptop, so data was collected only while it was on, and NDW traffic cannot be backfilled. The pipeline needed compute that runs around the clock. The workload is two Python containers running about a minute per hour, within a free-tier budget, a subscription limited to five regions, and the requirement to reach storage without stored credentials.

## Decision

The pipeline runs on one Azure VM: **Standard_B2ats_v2** (2 vCPUs, 1 GiB), Ubuntu 24.04 LTS with Trusted Launch, a 64 GiB Premium SSD (P6), in **Poland Central**. Its list price is about €6.94 per month for compute and €9.88 for the disk (Azure pricing calculator, October 2026), both covered by the free offer, which includes 750 hours per month of this size and a P6 disk. A VM runs the existing images unchanged, gives full control over SSH and costs nothing; a managed container service would add a new deployment model for a workload that needs none of its scaling.

The ingestion containers run permanently with their own hourly loop under `--restart unless-stopped`, rather than via cron as the course suggests. With cron they would exit after each run, erasing the in-memory `/health` state from Day 2 every hour. A reboot test confirmed both return on their own.

Access is restricted at every layer. SSH uses an Ed25519 key protected by a passphrase, with password login disabled; the network security group opened no ports except SSH from the developer's IP addresses (ADR-005 adds the dashboard port). The VM's managed identity holds Storage Blob Data Contributor on the `airbreda-raw` container only, stricter than the course's account-wide scope, and the logs show `"method": "managed_identity"`. Ingestion uses a database user, `airbreda_ingest`, limited to SELECT, INSERT and UPDATE on `sensor_readings`. The database firewall admits the VM's IP and the developer's IPs, with access for all Azure services off. The VM's `.env` is created on the VM itself with permissions `600` and is never committed.

The Redis queue from ADR-002 is dropped for this deployment: on one VM with no second consumer, it adds something to run and monitor without benefit. It returns when a second consumer needs the readings, or when ingestion outgrows one VM.

## Consequences

The pipeline runs continuously at no cost and recovers from crashes and reboots. The VM is a single point of failure, and OS security is the operator's responsibility, partly handled by Ubuntu's automatic security updates (the unattended-upgrades service is active on the VM).

The unanticipated concern was capacity: the course's size, B1s, and every free B-series size were unavailable to the subscription in Italy North, where the database and storage run. The VM therefore runs in Poland Central; each ingestion run sends only a few kilobytes across regions, so cost and latency are negligible, but either region failing now stops the pipeline. Smaller surprises followed: creating the VM without a network security group would have left no rule to open SSH, because the public IP blocks inbound traffic by default; firewall rules must be added per network; only 842 MiB of memory is usable, so a 2 GB swap file guards image builds against running out of memory; and Docker log rotation (three 10 MB files per container) prevents logs from filling the disk.

At 50 corridors with five-minute ingestion, NDW's single national file does not grow, but Luchtmeetnet calls would reach 50 per five minutes, half the API's fair-use limit. At that scale, ingestion would move to scheduled, scale-to-zero container jobs (Azure Container Apps jobs), a durable queue (Azure Service Bus) would be reintroduced, and the database would move to the General Purpose tier. Kubernetes would only be justified for many independently scaling services.
