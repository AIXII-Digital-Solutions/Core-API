# AIXII platform docs

Documentation for the three-service platform (core-api + external-worker + file-processor).

- **[platform-overview.md](platform-overview.md)** — **start here.** The orientation map across all four
  repos (core-api / external-worker / file-processor / db-contract): what exists, where it lives, what each
  part is for, and why. Read this before diving into any one service.
- **[architecture.md](architecture.md)** — how each service works, how they communicate, the
  shared contracts (job status, queues, scheduler registry, API tokens), the Redis keyspace.
- **[operations.md](operations.md)** — how to run and configure each service, the env reference,
  migrations, running several replicas on different servers, the control-plane admin APIs
  (scheduler / queues / tokens), and logging.
- **[capacity-control.md](capacity-control.md)** — portal-facing start/stop of the Power BI Embedded
  Azure capacity (`/api/v1/capacity/*`, scope `capacity:admin`): contract, `PBIE_*` config, Azure
  provisioning, the 401/403/409/502/503 codes, and credential rotation.

For agent/Claude-Code guidance see `../CLAUDE.md`. The schema source of truth is `../db-contract/`.
