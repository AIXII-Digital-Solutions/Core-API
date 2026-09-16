# Secrets — env or Vaultwarden

Owner: core-api. Code: `app/Config/secrets.py`, wired into `app/Config/config.py` (`DBSettings`,
`require_secret`) and `app/settings.py`. Health check: `tools/check_secrets.py`.

Callers only ever see `require_secret("DB_PASSWORD")`. Whether that value came from an environment
variable or from a self-hosted Vaultwarden is a deployment decision (`SECRETS_BACKEND`), not
something the calling code knows or can depend on.

## The nine managed secrets

| Key | Default vault item | Field |
|---|---|---|
| `DB_USER` | `aixii-postgres` | `username` |
| `DB_PASSWORD` | `aixii-postgres` | `password` |
| `REDIS_USER` | `aixii-redis` | `username` |
| `REDIS_USER_PASSWORD` | `aixii-redis` | `password` |
| `SERVICE_TOKEN` | `aixii-core-api-service-token` | `password` |
| `FILE_PROCESSOR_TOKEN` | `aixii-file-processor-token` | `password` |
| `AEROAPI_KEY` | `aixii-flightaware-aeroapi` | `password` |
| `AVIATION_EDGE_API_KEY` | `aixii-aviationedge` | `password` |
| `AVIATION_EDGE_EXTRA_API_KEY` | `aixii-aviationedge-extra` | `password` |

**The field is derived, not configured.** A `…_USER` key reads the item's `username`, every other key
its `password`, so a deployment only ever names the ITEM. `BW_FIELD_<KEY>` exists for the one case
that cannot be derived: a value kept in a CUSTOM field.

Credentials that belong together (postgres user+password, redis user+password) share ONE login item
and use its native `username`/`password` fields — that is what a login item is for, and `bw get`
addresses those fields directly without custom-field lookups.

**The mapping is topology, not a secret.** It lives in `_ITEM_MAP` in code and is overridable per key
with `BW_ITEM_<KEY>` / `BW_FIELD_<KEY>`. Keeping it visible is what makes a failure readable —
"item 'aixii-postgres' not found" instead of "something went wrong". The table above is the code's
DEFAULT naming; the AIXII vault uses its own item names, which the deployment sets in its env file
(`BW_ITEM_DB_PASSWORD=db_api`, …). `python tools/check_secrets.py` is how you confirm a host's names.

## A key the code does not know about

`BW_ITEM_<KEY>` also **declares** a key: set it for a key that is not in `_ITEM_MAP` and that key is
resolved from the vault too — `declared_keys()` (and therefore `check_secrets`) includes it. This is
how `MS_WEBHOOK_SECRET` and `PBIE_CLIENT_SECRET` reach the vault without a code change:

```bash
BW_ITEM_MS_WEBHOOK_SECRET=ms_graph_webhook      # login item's `password` field by default
```

## One item, several keys

A login item often holds a whole service principal: the id in `username`, the secret in `password`,
the rest in custom fields — `powerbi-aad` is exactly that, with a `tennant_id` beside the
credentials. Naming that item once per GROUP keeps the four values from drifting apart:

```bash
BW_ITEM_PBIE=powerbi-capacity
```

resolves `PBIE_CLIENT_ID` from `username`, `PBIE_CLIENT_SECRET` from `password`, `PBIE_TENANT_ID`
from the custom field `tennant_id` and `PBIE_SUBSCRIPTION_ID` from `subscription_id`. A per-key
`BW_ITEM_<KEY>` still wins for a value that lives elsewhere, and `BW_FIELD_<KEY>` renames a field an
item spells differently. `PBIE_RESOURCE_GROUP` and `PBIE_CAPACITY_NAME` are not credentials — they
say which capacity to act on — and stay in the env file.

Groups live in `_GROUP_KEYS`, the custom-field names in `_FIELD_MAP` (`Config/secrets.py`).

They have no built-in item on purpose: a default naming an item that a given vault does not have
would fail every boot on the hosts that still keep the value in their env file. **Without a name,
such a key is a plain environment variable** — the old behaviour, unchanged. The mapped keys never
fall back like that; that asymmetry is the whole point.

`PBIE_CLIENT_SECRET` is further marked OPTIONAL (`_OPTIONAL_KEYS`): it gates the `/capacity`
endpoints, which answer 503 without it. If its item is named but missing, the resolver warns and
falls back to the environment instead of failing the boot, and `check_secrets` reports it as
"not configured" rather than a failure. Every other mapped key stays fail-closed. A vault that is
unreachable, a bad credential or a TLS error still raises for all of them — "the vault is broken"
must never look like "the feature is off".

## How it is wired

`DB_USER` / `DB_PASSWORD` / `REDIS_USER` / `REDIS_USER_PASSWORD` are resolved inside `DBSettings.
__init__` and passed to pydantic explicitly — **never written into `os.environ`**, because anything
in `os.environ` is inherited by every subprocess the service ever spawns, which is the leak the vault
exists to prevent. The provider caches values, so the five `DBSettings()` call sites cost one
resolution between them.

`SERVICE_TOKEN` and `FILE_PROCESSOR_TOKEN` come through `require_secret` in `app/settings.py`.

The three provider API keys are **not** read at startup. core-api itself never calls those providers
— the loaders in `_admin/` and the worker services do — so they resolve lazily via
`Config.secrets.require_secret(...)`. A key nothing asks for must never be able to fail a boot.

The vault is locked on shutdown from the app lifespan (`middlewares.py`).

## Behaviour change to know about

Under `SECRETS_BACKEND=vaultwarden`, `SERVICE_TOKEN` and `FILE_PROCESSOR_TOKEN` become **required**.
Under `env` they still default to `""` exactly as before.

This is deliberate. Fail-closed means never substituting a default for a credential that failed to
resolve, and a deployment that runs a vault is production — where an empty `SERVICE_TOKEN` (which
silently denies every caller) is a misconfiguration, not a valid state.

## Operating it

```bash
python tools/check_secrets.py                 # every managed key: KEY -> OK (length N)
python tools/check_secrets.py DB_PASSWORD     # just these
```

It never prints a value, has no other side effects, and exits with the number of failures, so it
works as a deploy gate or a healthcheck. Run it first on any new host.

### Vault-side setup

1. A **service account**, never a personal one (`aixii-bot@…`), owned by an org admin. A personal
   account dies with the employee and takes production with it.
2. Secrets live in an **organization collection** shared with the bot user, as login items, one per
   credential. Give the bot **read-only** access if your Vaultwarden version supports it.
3. The bot's personal API key (Settings → Security → View API Key) gives `BW_CLIENTID` /
   `BW_CLIENTSECRET`; its master password is `BW_PASSWORD`.

Those three bootstrap secrets must still be injected some other way (compose `env_file`, systemd
credentials, a k8s Secret). **The vault removes nine secrets from the config and adds three** — it
pays off here because the credentials are shared across services and rotate, but for a service with
one secret it would not be worth it.

## Deployment (Docker)

The image ships the Bitwarden CLI, **pinned**:

| | |
|---|---|
| CLI version | **2026.6.0** (`cli-v2026.6.0`, released 2026-06-25) |
| Validated against Vaultwarden | _(record your server version here — see the note below)_ |
| Asset | `bw-linux-<ver>.zip` / `bw-linux-arm64-<ver>.zip`, one file `bw` at the archive root |
| sha256 amd64 | `392549496c712ab86bfbd6c27302df9fd2c431cfc7a47e26941ac3e3893f4d27` |
| sha256 arm64 | `626156e0ca60606c85b5b8ede0dd4e546b886a36e7f827b81d8cd5b8b487ee7c` |
| Installed at | `/usr/local/bin/bw` (`BW_CLI_PATH` is set in the image) |
| CLI state | named volume `bw_state` → `/var/lib/aixii/bw` (`BW_APPDATA_BASE`) |

**Why 2026.6.0 and not the newest.** 2026.7.0 is the release that moved to the strict WASM SDK and
rejects the cipher payload served by Vaultwarden < 1.37.0. 2026.6.0 is the last one before that
change, so it works against Vaultwarden on *either* side of 1.37.0 — the safe choice when the server
version is not pinned in lockstep. Once your Vaultwarden is confirmed ≥ 1.37.0 you can move the pin
forward; do it deliberately, record the pairing in the table above, and wipe the state volume.

GitHub publishes no checksum file for these assets, so the two hashes above were computed from the
released artefacts and are baked into the Dockerfile. **They must be updated together with
`BW_VERSION`** — a version bump with a stale hash fails the build, which is the intended behaviour.

```bash
docker compose up -d --build                       # uses the pinned version
docker build --build-arg BW_VERSION=2026.7.0 \
             --build-arg BW_SHA256=<new-hash> .    # deliberate bump
docker compose down && docker volume rm core-api_bw_state   # after ANY version change
```

The state volume is **named, not a bind mount**: it holds the login refresh token, so it should not
sit in the repo tree. It is persisted so a restart skips the login round-trip, and it is one volume
per container — two processes sharing a `bw` state directory corrupt each other.

`entrypoint.sh` can resolve the **boot-critical** secrets before starting the app when
`SECRETS_BACKEND != env`, but this is **off by default** (`CHECK_SECRETS_ON_BOOT`): it is a whole
extra vault round trip in its own process, and `app/main.py` now reports the same failure itself as
one classified `[core-api] FATAL: …` line instead of a traceback. Turn it on while setting a host up.
It deliberately checks only `DB_USER`, `DB_PASSWORD`, `REDIS_USER`,
`REDIS_USER_PASSWORD`, `SERVICE_TOKEN`, `FILE_PROCESSOR_TOKEN`, `MS_WEBHOOK_SECRET` — core-api never
calls the three data providers, so a missing `AEROAPI_KEY` must not block a boot. It costs one extra unlock+sync (a few
seconds); set `CHECK_SECRETS_ON_BOOT=false` to skip it.

## Hazards — read before deploying this

* **The `bw` version is pinned in the Dockerfile — keep it that way.** This is the expensive one. The
  CLI is normally installed from an unversioned "latest" URL, so any image rebuild silently upgrades
  it. Bitwarden CLI **2026.7.0** moved to a strict WASM SDK that rejects the legacy cipher payload
  served by **Vaultwarden < 1.37.0**; every `bw get` then dies with
  `invalid type: JsValue(Object({...})), expected a string` and a service that resolves secrets at
  startup goes into a restart loop. You are running a client against a reimplementation of the
  Bitwarden server, and the two drift. See the deployment table above for the current pin.
* **After changing the pinned version, wipe the state directories** — an older CLI cannot read a
  newer one's `data.json`.
* **A fresh state dir reports `serverUrl: null`.** The provider treats null as "differs" and runs
  `config server` anyway; without that, `bw login` silently targets the bitwarden.com **cloud** and
  fails with a confusing auth error against credentials that are perfectly valid for your server.
* **`bw` is slow, and it is slow PER CALL** — every invocation starts Node and decrypts the whole
  vault: measured at 4-5 s each, so six keys resolved one by one took ~37 s of boot. The provider
  therefore takes ONE snapshot (`bw list items`) per process and answers every key from it; the
  second key onwards costs nothing. Values are cached for the process lifetime; never resolve per
  request.
* **Several processes must not resolve secrets at once.** They share the CLI state directory, and
  concurrent `bw` runs corrupt each other — the symptom is a `bw get` that exits 0 with EMPTY output,
  i.e. a perfectly good credential reported as "field is empty", in a different process each time.
  This bit production when the API went to four uvicorn workers: each spawned worker opened the vault
  for itself. `app/main.py` now resolves everything in the parent and hands it to the workers
  (`secrets.hand_to_child_processes`), so only one process ever talks to `bw`.
* **Concurrency needs isolated state dirs.** `bw` keeps login state, the session and an encrypted
  vault cache in one directory, and two processes sharing it corrupt each other. The provider sets
  `BITWARDENCLI_APPDATA_DIR` to `<BW_APPDATA_BASE>/bw-<scope>`; give each concurrent process its own
  `BW_SCOPE`, and persist the directory across restarts so a restart skips the login round-trip.
* **Keep the `env` backend working.** Local dev, CI and incident response all need a path that does
  not touch the vault.

## Security invariants held by the implementation

Each exists because the alternative leaks; there are tests for all of them.

1. **Secrets reach `bw` only through the child-process environment, never argv** — `argv` is visible
   to every user on the box via `ps` and lands in shell history and process accounting. `--apikey`
   and `--passwordenv BW_PASSWORD` (which takes the NAME of a variable, not its value) are the
   mechanisms that make this possible.
2. **The child environment is stripped before anything is added to it**: the bootstrap variables and
   `NODE_OPTIONS` / `NODE_DEBUG` are removed — `bw` is a Node binary and those two can inject code
   into it.
3. **Fail closed.** Every failure raises a typed `SecretsError`. Nothing falls back to an empty
   value, a default, or a cached-from-last-time value. `bw get` exiting 0 with empty output means the
   field exists but is empty — a configuration error, never an empty string handed to a caller.
4. **Nothing secret reaches a log.** Every CLI message goes through a scrubber that replaces the
   known values with `***`. The length cap is generous on purpose: a 200-character cap once hid the
   operative half of a CLI crash message.
5. **`Secret` masks its own `repr`/`str`**, so accidental interpolation of the master password,
   client secret or session key into a log line is structurally impossible. `require()` deliberately
   returns a plain `str` — its consumers are pydantic fields and third-party clients that type-check
   or serialise their inputs, where a subclass lying about its repr causes worse bugs than it
   prevents.
6. **Unlock at most once per process, lazily**, behind a lock, with the cache rolled back on failure
   so a half-failed unlock cannot leave the provider "unlocked".
7. **Every call is time-boxed** (60 s) and the process **tree** is killed on timeout — descendants
   are collected first, because after a forced kill they are re-parented and become unfindable.
8. **`locked` → clear the session and retry exactly once**, so a long-lived worker survives an
   expired session without a restart while a genuinely broken vault cannot spin.
9. **A `.cmd`/`.bat` shim re-parses its arguments**, so item names containing `" % & | < > ^ ( ) !`
   or newlines are rejected rather than passed through. Point `BW_CLI_PATH` at a native executable.

## Error taxonomy

Operators need to know *whose* problem it is, so failures are classified rather than propagated raw:

| Class | Means |
|---|---|
| `SecretsNotInstalled` | `bw` missing from PATH / `BW_CLI_PATH` wrong |
| `SecretsUnreachable` | network, Vaultwarden down, or the call timed out |
| `SecretsTlsError` | certificate not trusted — set `NODE_EXTRA_CA_CERTS` |
| `SecretsAuthError` | wrong `BW_CLIENTID` / `BW_CLIENTSECRET` |
| `SecretsUnlockError` | wrong master password, or no usable session returned |
| `SecretsLocked` | session expired — cleared and retried once, then raised |
| `SecretsItemNotFound` | wrong or ambiguous item name, or the field is empty |
| `SecretsCliError` | anything else, with a scrubbed CLI snippet |
