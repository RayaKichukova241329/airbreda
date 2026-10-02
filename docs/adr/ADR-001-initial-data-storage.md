# ADR-001: Initial Data Storage Strategy

**Status:** Accepted
**Date:** 2026-09-30

## Context

AirBreda combines two public data sources: hourly NO₂ averages from Luchtmeetnet station NL10240 (Breda-Tilburgseweg), and one-minute traffic measurements from four NDW sites at the A27 interchange (`hrl`, `hrr`, `vwd`, `vwa`). Both are time series: a source, a timestamp and a value, arriving almost only as inserts for the latest period.

Three needs pull in different directions. The dashboard and the model need both sources combined by time, which favours a queryable store. The model needs history, but NDW only exposes current traffic, so anything not saved while live is lost. And readings arrive repeatedly: Luchtmeetnet returns its last 50 hours on every call, and the scripts retry after failures. The volume is small: about 79,000 rows per year in this design (one NO₂ value plus flow and speed for four sites, every hour; the course estimates 96,000 with three air-quality components), and the budget is an Azure for Students subscription with a free PostgreSQL tier.

## Decision

Processed readings are stored in Azure Database for PostgreSQL Flexible Server (Burstable B1ms, Italy North), in `sensor_readings` with the primary key `(station_id, timestamp, component)`. AirBreda's core question requires joining two sources on time, which a relational database does natively, and the key lets the database itself guarantee that each reading is stored once. PostgreSQL was chosen over Azure SQL Database because the schema and conflict handling use PostgreSQL syntax, the tier is free, and the same engine is offered by the other major cloud providers; the course instructor confirmed this choice. Azure Cosmos DB for NoSQL was rejected: horizontal scaling and a flexible schema solve problems AirBreda does not have, while a time-based join across two sources is not native to a document store.

Ingested traffic files are stored in Azure Blob Storage (LRS, private container `airbreda-raw`), one per site per hour at `ndw/YYYY-MM-DD/HH-<site>.csv`, named by measurement time in UTC. The bucket is the only durable record of past traffic and allows replaying history through corrected code, following the Kappa approach of one processing path with reprocessing by replay. The database can be rebuilt from the bucket, not the reverse.

Ingestion is at-least-once, and every write is made idempotent with `INSERT ... ON CONFLICT DO NOTHING`; a first run stored 50 readings and an immediate second run stored none. Missing values are stored as `NULL`, so gaps stay visible, and traffic files are overwritten per hour. Duplicates are harmless; lost readings would leave permanent gaps in the training data.

## Consequences

The setup is simple and free: the database lists at about €17 per month (Azure pricing calculator, October 2026), covered by the free offer, and the bucket's 2,900 small files per month cost cents. Horizontal write scalability is given up, which AirBreda will not need: a benchmark of plain PostgreSQL (Tiger Data, formerly Timescale, 2017) only showed insert rates degrading after about 50 million rows, centuries away at AirBreda's volume, and time-based partitioning is an upgrade path on the same engine.

Three risks are accepted: the bucket holds parsed summaries rather than raw NDW XML, so a parsing bug cannot be corrected from it; traffic is one one-minute snapshot per hour, which makes it noisy; and the database is a single instance (see ADR-003).

**Update (Days 2 to 4):** traffic readings are now also written to the database, with the data quality rules of ADR-002. For the deployed services, the admin login and storage key used initially were replaced by restricted database users and a managed identity (ADR-004, ADR-005); the admin login and key remain on the developer's laptop for setup and training.
