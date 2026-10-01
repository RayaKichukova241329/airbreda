# ADR-002: Messaging Architecture

**Status:** Accepted. The queue was removed from the deployed system by ADR-004; the data quality rules remain in force.
**Date:** 2026-10-01

## Context

After Day 1, each ingestion script fetched and wrote directly to storage, so any future consumer of the readings, such as the model or an anomaly detector, would have to be wired into the ingestion code. A broker between producers and consumers removes that coupling. Delivery is at-least-once by nature: 49 of the 50 NO₂ readings in each hourly run are repeats. Both sources also produce bad data in different forms: Luchtmeetnet returns nulls or values stuck for hours, and NDW reports `speed = -1` when a lane has no valid speed.

## Decision

Every reading is published as JSON to a Redis list, `readings`, alongside the direct writes to the database and bucket rather than instead of them. Redis runs as a third container on the private Docker network; each message carries a `source` field because both producers share the list. One run publishes 54 messages (50 NO₂, 4 NDW), verified with `LLEN`. Redis was chosen because it establishes the pattern with the least to operate, running locally with no account or configuration. A production deployment would use Azure Service Bus, which stores messages durably, dead-letters failing messages and needs no broker to run. Because nothing consumes the queue yet, the database remains the system of record.

If the broker goes down, no reading is lost. Redis runs without a volume, so queued messages vanish on restart, but each reading is already stored. Publishing and storing are independent: a failed publish is logged as `publish_failed` without blocking the database write, and vice versa.

A Luchtmeetnet reading that is null, or part of three or more consecutive hours with an identical value, is kept with `is_flagged = TRUE` and logged as a `DATA_QUALITY_ERROR` warning, because a dropped row would make the history look complete when it is not. An NDW speed of `-1` is a sentinel, not a measurement, so that site's speed row is not written, while its flow row and bucket file are kept. Events are counted per source; more than 10 within an hour raises one `BAD_DATA_THRESHOLD_EXCEEDED` error.

## Consequences

New consumers can be added without touching ingestion, and consumers must be idempotent, which the database already is. The cost is one more container and a dual write in which queue and database could briefly disagree, acceptable while the database is the source of truth.

The ingestion logs showed that the NDW rule is too blunt. At 00:06 and 00:09 UTC on 1 October, every site reporting `speed = -1` also reported zero vehicles, while the one site with traffic had a valid speed: at night, `-1` mostly means no vehicle passed. Starting over, the asymmetry would be kept, since a missing NO₂ hour is information about the air while a sentinel speed is not, but the NDW rule would be refined: `-1` with zero flow is a missing speed, not bad data, and only `-1` with traffic present is an error. With hourly polling, NDW can also produce at most four warnings per hour, so its threshold of 10 cannot be reached.
