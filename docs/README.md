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
- **[secrets.md](secrets.md)** — how the nine managed credentials are resolved at runtime:
  `SECRETS_BACKEND=env` (default) or a self-hosted Vaultwarden driven through the Bitwarden CLI.
  The item mapping, `tools/check_secrets.py`, the security invariants, and the version-pinning
  hazard that will put a service into a restart loop if you ignore it.
- **[capacity-control.md](capacity-control.md)** — portal-facing start/stop of the Power BI Embedded
  Azure capacity (`/api/v1/capacity/*`, scope `capacity:admin`): contract, `PBIE_*` config, Azure
  provisioning, the 401/403/409/502/503 codes, and credential rotation.
- **[insured-fleet.md](insured-fleet.md)** — the insured aircraft across the `ref` / `fleet` /
  `leasing` / `policy` schemas plus the `audit` change log: which column holds which field of
  the source specification, why leasing and policy each carry the same three limits (required
  vs provided cover), how a yearly policy renewal is modelled, and how the two kinds of history
  work, and the API over all of it (`/ref/*`, `/fleet/*`, `/leasing/*`, `/policy/*`, `/history/*`,
  scopes `insurance:read` / `insurance:write`). Replaced the single `insurance` schema, which was
  dropped empty together with its claims tables.

For agent/Claude-Code guidance see `../CLAUDE.md`. The schema source of truth is `../db-contract/`.
