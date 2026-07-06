# AIXII database — granting access (self-service runbook)

Practical, copy-paste recipes for handing out database access **after** the initial cluster was built
by `db-aixii-setup.sql`. Read this when you add a new schema, onboard a new consumer, or need to give
an existing role more (or less) than it has today. Target: **PostgreSQL 17**.

For the *why* behind the design (one `aixii` data DB with per-domain schemas + a separate `service`
DB) see [`db-aixii-setup.md`](db-aixii-setup.md). This file is the *how-to-operate-it* companion.

---

## 0. The one rule that makes everything else simple

> **Never grant privileges to a login user directly. Grant to a *group role*; the login user is a
> member of the group and inherits everything automatically.**

That is the whole model. Login users (`bi_reader`, `svc_api`, …) own **no** privileges of their own —
they are just members of `grp_*` bundles. So "give X access to schema Y" almost always means "grant
schema Y to the *group* that X belongs to", and every member of that group gets it for free.

This also means: to change what a consumer can see, you usually **don't touch the consumer** — you
touch its group. And you run every command below **as `developer`** (owner of all schemas/tables) or
as the `postgres` superuser.

---

## 1. The roles at a glance

### Group roles (NOLOGIN — privilege bundles)

| Group | What it grants | On which schemas |
|---|---|---|
| `grp_aixii_read` | `USAGE` + `SELECT` | **all** `aixii` schemas (sources + `api` + any read-exposed schema like `forecast`) |
| `grp_aviation_write` | `USAGE` + DML (`SELECT/INSERT/UPDATE/DELETE`) + sequence usage | the source schemas: `flightradar`, `aviationedge`, `cirium`, `airlabs`, `icao` |
| `grp_api_write` | `USAGE` + DML + sequence usage | `api` only |
| `grp_service_write` | `USAGE` + DML + sequence usage | `public` schema of the **`service`** DB |

### Login users (what actually connects) + their group membership

| User | Purpose | Member of | Net access |
|---|---|---|---|
| `bi_reader` | PowerBI / BI read-only | `grp_aixii_read` | SELECT on all read-exposed `aixii` schemas. **No** CONNECT to `service`. |
| `svc_external_worker` | external-worker service | `grp_aviation_write`, `grp_service_write` | DML on sources + `service`; SELECT on `api` (granted directly, see setup script). |
| `svc_file_worker` | file-processor service | `grp_aviation_write`, `grp_service_write` | DML on sources + `service`. |
| `svc_api` | Core-API runtime | `grp_aixii_read`, `grp_api_write`, `grp_service_write` | SELECT everywhere in `aixii` + DML on `api` + DML on `service`. |
| `developer` | owner / migrator (SUPERUSER) | — (owns everything) | Everything. Alembic runs as this role. |

**Guiding principle:** migrations run as `developer`; runtime services get **DML only**, BI gets
**SELECT only**. `ALTER DEFAULT PRIVILEGES FOR ROLE developer …` then auto-grants every *future* table
to the right group, so you don't re-grant after each migration (caveats in §6).

---

## 2. Two things every grant needs (don't forget either)

A schema grant is **two** privileges, and missing one is the usual "permission denied" cause:

1. **`USAGE ON SCHEMA`** — the right to *enter* the schema (resolve object names). Without it, nothing
   inside is reachable even with table grants.
2. **`SELECT` / DML `ON TABLES`** — the right to read/write the objects themselves.

And for anything you want to apply to **future** objects (tables created by later migrations), a third:

3. **`ALTER DEFAULT PRIVILEGES FOR ROLE developer IN SCHEMA … GRANT … TO <group>`** — so the next
   migration's tables are auto-granted. Default privileges are keyed to the **creating role**
   (`developer`) — that's why the `FOR ROLE developer` clause is mandatory.

`GRANT … ON ALL TABLES` only touches objects that **exist right now**; `ALTER DEFAULT PRIVILEGES` only
touches objects created **later**. You almost always want **both** (existing + future) → so every
recipe below runs the pair.

---

## 3. Recipe A — expose a NEW schema for reading (the common case)

Use this when a new schema has been created by a migration (e.g. `forecast`) and you want BI / anything
in `grp_aixii_read` to read it. Run **as `developer`**, on the `aixii` database:

```sql
\c aixii

-- 1) enter the schema  2) read existing tables  3) read all future tables
GRANT USAGE ON SCHEMA forecast TO grp_aixii_read;
GRANT SELECT ON ALL TABLES IN SCHEMA forecast TO grp_aixii_read;
ALTER DEFAULT PRIVILEGES FOR ROLE developer IN SCHEMA forecast
  GRANT SELECT ON TABLES TO grp_aixii_read;
```

That's it — every member of `grp_aixii_read` (today: `bi_reader`, `svc_api`) can now read `forecast`.
No per-user command needed.

**Repeat on the dev mirror** if that schema exists there too:

```sql
\c aixii_dev
GRANT USAGE ON SCHEMA forecast TO grp_aixii_read;
GRANT SELECT ON ALL TABLES IN SCHEMA forecast TO grp_aixii_read;
ALTER DEFAULT PRIVILEGES FOR ROLE developer IN SCHEMA forecast
  GRANT SELECT ON TABLES TO grp_aixii_read;
```

> Generalise: to expose *any* schema for reading, replace `forecast` with the schema name. To expose it
> to a **different** audience than BI, replace `grp_aixii_read` with the group you want (or make a new
> group — §7).

---

## 4. Recipe B — expose a NEW schema for writing

For a schema that a worker must write to. Pick the group that already models that write audience
(`grp_aviation_write` for source-style ingest, `grp_api_write` for API-owned tables), or make a new one
(§7). Run **as `developer`**, on `aixii`:

```sql
\c aixii

GRANT USAGE ON SCHEMA <schema> TO <write_group>;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES    IN SCHEMA <schema> TO <write_group>;
GRANT USAGE,SELECT                ON ALL SEQUENCES IN SCHEMA <schema> TO <write_group>;
ALTER DEFAULT PRIVILEGES FOR ROLE developer IN SCHEMA <schema>
  GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO <write_group>;
ALTER DEFAULT PRIVILEGES FOR ROLE developer IN SCHEMA <schema>
  GRANT USAGE,SELECT ON SEQUENCES TO <write_group>;
```

The two **`ON SEQUENCES`** lines are what make `bigserial`/`GENERATED … AS IDENTITY` inserts work — a
writer without `USAGE,SELECT` on the sequence gets "permission denied for sequence …" on INSERT.
Read-only groups never need sequence grants (§3 has none).

> A writer typically also needs to *read* what it writes — DML already includes `SELECT`, so a schema in
> a write group does **not** additionally need to be in `grp_aixii_read` for that writer. Add
> `grp_aixii_read` only if you want the BI audience to also read it.

---

## 5. Recipe C — give an EXISTING login user access (add/remove from a group)

You rarely grant to a user directly. To change what a user can reach, change its group membership:

```sql
-- give svc_api the ability to also read everything BI reads (already true, shown as the pattern)
GRANT grp_aixii_read TO svc_api;

-- take a group away from a user (revokes everything that group provided, in one shot)
REVOKE grp_aviation_write FROM svc_file_worker;
```

Membership changes take effect on the user's **next new session** (existing pooled connections keep
their old privileges until they reconnect — bounce the service or its pool if you need it immediately).

If you ever truly need a **one-off** grant to a single user (not recommended — it bypasses the group
model and is easy to forget), it's the same statements as §3/§4 with the user name instead of a group.
Prefer creating/using a group.

---

## 6. Materialized views — the one gotcha to remember

`ALTER DEFAULT PRIVILEGES … ON TABLES` does **not** cover **materialized views** for *future* objects
(a PostgreSQL limitation — matviews are not "tables" for default-privilege purposes). Regular views and
plain tables are fine.

Consequence: when a migration adds a **new matview**, the default-privilege auto-grant will **miss it**,
and BI will get "permission denied" on that matview. Fix with a one-time re-grant after the migration —
`GRANT SELECT ON ALL TABLES` *does* pick up existing matviews, so just re-run it:

```sql
\c aixii
GRANT SELECT ON ALL TABLES IN SCHEMA <schema> TO grp_aixii_read;   -- re-catches new matviews too
```

Rule of thumb: **any time a migration adds a matview, re-run the `GRANT SELECT ON ALL TABLES` line for
that schema.** (Plain tables/views are handled automatically by default privileges — no action needed.)

---

## 7. Recipe D — create a NEW consumer or a NEW privilege bundle

### New login user (e.g. a second BI account, an analyst)

```sql
-- 1) create the login role (strong password; rotate via ALTER ROLE later)
CREATE ROLE analyst_jane LOGIN PASSWORD '<strong_generated_pw>';

-- 2) let it connect to the data DB (bi_reader-style: aixii only, NOT service)
GRANT CONNECT ON DATABASE aixii TO analyst_jane;      -- run while connected to any DB; DATABASE-level

-- 3) put it in the group whose access it should have — done, it inherits everything
GRANT grp_aixii_read TO analyst_jane;
```

No schema grants needed in step 3 — the group already carries them. Repeat step 2 for `aixii_dev` if
the account should reach dev. **Do not** grant `CONNECT ON DATABASE service` to a read-only/BI account.

### New group (a distinct access bundle, e.g. "read only the forecast schema, nothing else")

```sql
CREATE ROLE grp_forecast_read NOLOGIN;
-- then apply Recipe A but grant to grp_forecast_read instead of grp_aixii_read,
-- and add the desired users:  GRANT grp_forecast_read TO analyst_jane;
```

Use a new group when the existing bundles are too broad — e.g. a partner who should see `forecast` but
not `cirium`. Keep group roles `NOLOGIN`.

---

## 8. Verify what a role can actually do

```sql
-- who is a member of which group?
\du+          -- psql: lists roles and their memberOf

-- effective table privileges for a role in a schema
SELECT table_schema, table_name, privilege_type
FROM   information_schema.role_table_grants
WHERE  grantee = 'grp_aixii_read' AND table_schema = 'forecast'
ORDER  BY table_name, privilege_type;

-- does a role have USAGE on the schema? (the easy-to-miss half)
SELECT has_schema_privilege('grp_aixii_read', 'forecast', 'USAGE');

-- default privileges currently configured (what future tables will inherit)
\ddp          -- psql meta-command: lists ALTER DEFAULT PRIVILEGES entries

-- ultimate check: can this *user* read this table right now?
SELECT has_table_privilege('bi_reader', 'forecast.final_1', 'SELECT');
```

`has_*_privilege(...)` resolves through group membership, so it's the truest "will it work" test.

---

## 9. Revoke / tighten access

```sql
-- stop a group from reading a schema (mirror of Recipe A — do all three)
REVOKE SELECT  ON ALL TABLES IN SCHEMA forecast FROM grp_aixii_read;
REVOKE USAGE   ON SCHEMA forecast              FROM grp_aixii_read;
ALTER DEFAULT PRIVILEGES FOR ROLE developer IN SCHEMA forecast
  REVOKE SELECT ON TABLES FROM grp_aixii_read;

-- remove a user entirely from an audience (fastest, keeps the schema grants intact for others)
REVOKE grp_aixii_read FROM analyst_jane;

-- rotate a password
ALTER ROLE bi_reader PASSWORD '<new_strong_pw>';   -- then update the PowerBI gateway data source

-- drop a user (must own nothing and be a member-only role; revoke memberships first)
REVOKE grp_aixii_read FROM analyst_jane;
DROP ROLE analyst_jane;
```

Don't forget the `ALTER DEFAULT PRIVILEGES … REVOKE` line — otherwise the group keeps auto-gaining
access to **future** tables in that schema even after you revoked the current ones.

---

## 10. Keep the master script in sync

The recipes above are for **operating** a live cluster. If you also want a **rebuilt-from-scratch**
cluster to end up in the same state, add the schema to `db-aixii-setup.sql` (and its narrative twin
`db-aixii-setup.md`):

- add the schema name to the read loop's array
  `ARRAY['flightradar','aviationedge','cirium','airlabs','icao','api']` (both the `aixii` and
  `aixii_dev` blocks), **and**
- if the schema is created by a **migration** (like `forecast`, `CREATE SCHEMA IF NOT EXISTS forecast`),
  either also add a `CREATE SCHEMA IF NOT EXISTS forecast AUTHORIZATION developer;` line **before** the
  grant loop (so the setup script is self-sufficient), or accept that the grant loop must run *after*
  the migration has created the schema.

Otherwise a future `db-aixii-setup.sql` re-run won't know about the schema and BI will silently lose
access on a rebuild.

---

## Appendix — worked example: `forecast` for `bi_reader`

The `forecast` schema (`history_1` / `future_1` / `final_1`, migration `d7e8f9a0b1c2`) exposed to BI.
`bi_reader` is already a member of `grp_aixii_read`, so this is exactly Recipe A — no user-level command:

```sql
\c aixii
GRANT USAGE ON SCHEMA forecast TO grp_aixii_read;
GRANT SELECT ON ALL TABLES IN SCHEMA forecast TO grp_aixii_read;
ALTER DEFAULT PRIVILEGES FOR ROLE developer IN SCHEMA forecast
  GRANT SELECT ON TABLES TO grp_aixii_read;

-- dev mirror
\c aixii_dev
GRANT USAGE ON SCHEMA forecast TO grp_aixii_read;
GRANT SELECT ON ALL TABLES IN SCHEMA forecast TO grp_aixii_read;
ALTER DEFAULT PRIVILEGES FOR ROLE developer IN SCHEMA forecast
  GRANT SELECT ON TABLES TO grp_aixii_read;
```

Verify:

```sql
SELECT has_table_privilege('bi_reader', 'forecast.final_1', 'SELECT');   -- expect: t
```

If a later migration adds a **matview** in `forecast`, re-run the middle line (§6):
`GRANT SELECT ON ALL TABLES IN SCHEMA forecast TO grp_aixii_read;`
