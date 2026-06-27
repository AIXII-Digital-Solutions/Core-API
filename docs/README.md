# AIXII platform docs

Documentation for the three-service platform (core-api + external-worker + file-processor).

- **[architecture.md](architecture.md)** — how each service works, how they communicate, the
  shared contracts (job status, queues, scheduler registry, API tokens), the Redis keyspace.
- **[operations.md](operations.md)** — how to run and configure each service, the env reference,
  migrations, running several replicas on different servers, the control-plane admin APIs
  (scheduler / queues / tokens), and logging.

For agent/Claude-Code guidance see `../CLAUDE.md`. The schema source of truth is `../db-contract/`.
