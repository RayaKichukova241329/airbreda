---
layout: default
title: AirBreda Architecture Design Document
---

# AirBreda: Architecture Design Document

AirBreda investigates whether traffic congestion at the A27 interchange near Breda pushes air pollution nearby above safe levels. The platform ingests two Dutch open data streams, hourly NO₂ averages from Luchtmeetnet station NL10240 (RIVM) and one-minute traffic counts from four NDW measurement sites at the interchange, stores them in Azure, trains a regression model on the collected data and serves predictions through an API and a dashboard. This document records the system as deployed, the decisions behind it and what they cost.

**Live system:** [http://20.215.185.196:8000](http://20.215.185.196:8000)  
**Repository:** [github.com/RayaKichukova241329/airbreda](https://github.com/RayaKichukova241329/airbreda)

## 1. Architecture diagram

![AirBreda architecture as deployed](diagrams/architecture-final.svg)

The diagram shows the system as deployed. Three containers run on one Azure VM in Poland Central: two ingestion services and a FastAPI dashboard, connected by a private Docker network. The database and the object storage run in Italy North. Every arrow that crosses a trust boundary is labelled with the identity it uses: the network security group admits only the dashboard port and SSH from the developer's IP; the VM's managed identity can access one storage container and nothing else; and the containers reach the database as two separate users, one of them read-only. The editable source is [`architecture-final.drawio`](diagrams/architecture-final.drawio). Earlier checkpoints show how the design evolved: [Day 1](diagrams/architecture-day1.svg), [Day 2](diagrams/architecture-day2.svg) and [Day 3](diagrams/architecture-day3.svg).

## 2. Architecture decision records

{% include_relative adr/ADR-001-initial-data-storage.md %}

{% include_relative adr/ADR-002-messaging-architecture.md %}

{% include_relative adr/ADR-003-resilience-strategy.md %}

{% include_relative adr/ADR-004-compute-strategy.md %}

{% include_relative adr/ADR-005-compute-and-deployment.md %}

{% include_relative adr/ADR-006-ml-serving-architecture.md %}

## 3. Trade-off justifications

**Storage.** A relational database was weighed against a document store (Azure Cosmos DB for NoSQL) and against SQL Server (Azure SQL Database). PostgreSQL was chosen because the core analysis joins two time series, the composite primary key enforces idempotent writes, and the free B1ms tier (list price about €17 per month) comfortably handles about 79,000 rows per year. Horizontal write scalability was given up; a benchmark only showed plain PostgreSQL slowing down after about 50 million rows. Object storage was added alongside the database because NDW traffic cannot be recovered once missed, at a cost of a few cents per month.

**Compute.** A single VM was weighed against a managed container service. The VM was chosen because it runs the existing images unchanged at no cost (Standard_B2ats_v2 with its disk, list price about €17 per month, covered by the free offer) and the workload is two hourly jobs and a lightly used dashboard. Automatic scaling, zero-downtime deployments and HTTPS were given up, along with any redundancy: the VM is a single point of failure.

**Messaging.** A broker between producers and consumers was weighed against direct writes. Redis was introduced on Day 2 to establish the pattern, publishing 54 messages per run alongside the direct writes, and removed on Day 3 because nothing consumed it. Decoupling for future consumers was given up in exchange for one fewer component to run; a managed queue, Azure Service Bus, would return at 50 corridors, when ingestion is split into scheduled jobs.

**Disaster recovery.** Backup and Restore was weighed against Pilot Light, Warm Standby and Active-Active. It was chosen because it costs nothing extra and every component is reproducible from Git and automatic database backups, giving an estimated recovery time of one to two hours. A shorter recovery time was given up, and with it every hour of NDW traffic during an outage. Moving up a tier would first require leaving the Burstable database tier, which supports neither replicas nor high availability.

## 4. Cloud provider rationale

*Written for a policy officer at the Municipality of Breda.*

AirBreda runs on Microsoft Azure, a service that rents out computers, storage and databases in Microsoft's data centres. Instead of buying and maintaining its own server, the project uses three rented building blocks: a small computer that collects the measurements every hour and runs the website, a database that keeps all readings in order, and a storage area that keeps a copy of every traffic file.

This is appropriate for a public-sector project in the Netherlands for three reasons. First, all data stays inside the European Union: the database and storage are in Microsoft's data centre in Italy, and the computer runs in Poland, so the data remains under European privacy law. The project also stores no personal information; it only handles public air quality and traffic figures. Second, security is built in. Access is restricted at every step: only the website is open to the public, only the developer can log in to the computer, and each part of the system can reach only the data it needs. No passwords are stored in the project's code. Third, the cost is low and predictable. During this prototype phase, Azure's student programme covers nearly all of the cost; the normal price of the same setup is roughly 34 euros per month, and the platform reports exactly what is used.

Switching to another provider would be possible but not free. The project deliberately uses widely available technology, a widely used open-source database and a standard way of packaging software, so its core would run on Amazon's or Google's cloud as well, and the municipality would not be locked in. What would be lost is the work of setting up the current environment: the access rules, the security settings and the tested deployment would have to be rebuilt and checked again, which would take time and carry the risk of new mistakes.

## 5. Cost estimate

Prices are Azure list prices in euros per month, from the Azure pricing calculator on 2 October 2026 (Poland Central for compute, Italy North for the database and storage). The current deployment is covered by the Azure for Students free offer, so its actual cost is close to zero.

| Component | Current (1 corridor) | At 10 corridors | At 50 corridors |
|---|---|---|---|
| Compute | 16.82 (VM B2ats_v2 6.94, P6 disk 9.88) | 16.82 (same VM) | 29.41 (Container Apps jobs 12.59, dashboard VM 16.82) |
| Database | 16.64 (B1ms, 32 GiB) | 16.64 (same server) | 143.89 (General Purpose D2ds v5, 64 GiB) |
| Object storage | 0.14 | 0.23 | 8.23 (about 1.75 million writes) |
| **Total** | **33.60** | **33.69** | **181.53** |

The calculator models Container Apps by web requests, so the scheduled jobs were calculated from its unit prices (€0.00002112 per vCPU-second, €0.00000264 per GiB-second) and free monthly allowances (180,000 vCPU-seconds and 360,000 GiB-seconds): about 8,760 runs of each job per month, every five minutes, giving 657,000 vCPU-seconds and 1,314,000 GiB-seconds. Storage write operations were rounded up to whole units of 10,000. Not included: the VM's public IP address and the Service Bus namespace that the 50-corridor design would add.

At 10 corridors with hourly ingestion, a single VM is still the right choice: the national NDW file is downloaded once per run regardless of the number of sites, ten Luchtmeetnet calls per hour are far below the fair-use limit, and the database grows to under a million rows per year. At 50 corridors with ingestion every five minutes, a single VM is no longer appropriate: it would become a single point of failure for 50 locations, and the workload calls for scheduled container jobs, a durable queue and a General Purpose database tier, as described in ADR-004. The database then becomes by far the largest cost, about 79% of the total.

## 6. Reflection

The decision I would be least confident defending to a senior engineer is running the whole system on a single VM, with Backup and Restore as the recovery tier. It is cheap and simple, but it carries a cost that is easy to overlook. NDW only publishes current traffic, so every hour the VM is down becomes a permanent gap in the training data. The pipeline also depends on two regions, because the VM runs in Poland Central while the data lives in Italy North, so a failure in either one stops it. To defend this choice with confidence, I would need three things I do not have yet: how often the system is actually unavailable, measured over weeks rather than days; how much the model's error grows for every missing hour of traffic; and what the smallest safeguard would cost. A small ingestion-only VM in a second region would cost about the same as the current one, roughly 17 euros per month at list price, and if outages turned out to be frequent, that would be a better use of money than upgrading the database tier.

The final model was trained on 18 hours of data, and its result was not what I expected: the traffic coefficient turned negative, so in this data more traffic goes with slightly less NO₂, and an R² of 0.03 means the model explains almost none of the variation. With so little data, these numbers describe the hours observed rather than a real effect, and no conclusion about traffic can be drawn from them: the sign of the traffic coefficient has already changed once, and earlier, a single added row moved the leave-one-out error from 47 to 6 µg/m³. With a full year of readings, the first improvement would be the traffic feature itself: NDW counts vehicles per minute and the pipeline keeps one minute per hour, so polling every five minutes and averaging would give a value that covers the same hour as the NO₂ average. I would add weather, above all wind speed, which affects how quickly NO₂ disperses, plus the day of the week, public holidays and the hour encoded as a cycle. I would evaluate with time-based splits, training on earlier months and testing on later ones, against a simple baseline such as "the same value as last hour", and only move to gradient-boosted trees if they clearly beat the linear model.

If AirBreda served the municipality in production, the first thing I would add is an automated deployment pipeline, with the infrastructure defined as code. Today, a release means training on my laptop, pushing to GitHub, logging in to the VM over SSH and rebuilding containers by hand, and the Azure resources were created by clicking through the portal. A pipeline, for example in GitHub Actions, would run the tests on every push, build the three images once, store them in a container registry and deploy exactly those images, so the VM would no longer build anything itself. Defining the VM, the network rules, the database and the storage in Bicep or Terraform would make the whole environment reproducible in minutes, which would also turn the Backup and Restore recovery from hours into a single command. The next steps follow from the same principle of removing manual work: secrets in Azure Key Vault instead of .env files, automatic alerts on the /health endpoints, and HTTPS in front of the dashboard.
