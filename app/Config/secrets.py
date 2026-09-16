"""Runtime secret resolution: plain environment, or a self-hosted Vaultwarden.

Callers only ever see ``require("DB_PASSWORD")``. Whether that value came from an environment
variable or from a vault is a deployment decision (``SECRETS_BACKEND``), not something the calling
code knows or can depend on.

Why a subprocess: there is no usable Vaultwarden client library. The supported integration is the
official Bitwarden CLI (``bw``) pointed at your own server, driven as a child process. The whole
design goal is that a secret exists in exactly two places — the vault, and the memory of the process
that needs it. Never in argv, never in a log line, never in a file, never in the image.

    service ──spawn──> bw status / login --apikey / unlock / sync / get ──HTTPS──> Vaultwarden
       ▲                        │
       └── value on stdout ─────┘     secrets reach bw ONLY through the child-process environment

The vault removes N secrets from the config and adds 3 (``BW_CLIENTID`` / ``BW_CLIENTSECRET`` /
``BW_PASSWORD``), which still have to be injected some other way. That is the irreducible bootstrap
problem; it pays off because N is large here and the credentials are shared across services.

Operational notes that are load-bearing — see docs/secrets.md before changing any of this:
  * ``bw`` is SLOW (unlock + sync is seconds). Values are cached in memory for the process lifetime;
    resolution is lazy, so a key nothing asks for never costs anything and never fails a boot.
  * The ``bw`` version must be pinned against the Vaultwarden version — the client is talking to a
    reimplementation of the Bitwarden server and the two drift.
  * ``SECRETS_BACKEND=env`` (the default) must keep working forever: local dev, CI and incident
    response all need a path that does not touch the vault.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

# NOTE: no imports from Config at MODULE level — Config/config.py imports this module (DBSettings
# asks it for the database credentials) and Config/Logger.py imports config, so a module-level
# `from .Logger import setup_logger` here would be a genuine import cycle. The logger is fetched
# lazily instead: by the time any of these functions runs, Config is fully loaded.

_logger: Optional[logging.Logger] = None


def _log() -> logging.Logger:
    """The project logger, resolved on first use — see the import-cycle note above.

    Everything logged here is a LIFECYCLE event (unlocking, syncing, recovering from an expired
    session) or a key NAME. Values never reach it, and CLI output is scrubbed before it does.
    """
    global _logger
    if _logger is None:
        try:
            from .Logger import setup_logger
            _logger = setup_logger("secrets")
        except Exception:                     # noqa: BLE001 - logging must never break resolution
            _logger = logging.getLogger("secrets")
    return _logger

_BOOTSTRAP_VARS = ("BW_CLIENTID", "BW_CLIENTSECRET", "BW_PASSWORD", "BW_SESSION")

# `bw` is a Node binary: these two can inject code into it, so they never reach the child.
_DANGEROUS_VARS = ("NODE_OPTIONS", "NODE_DEBUG")

_CALL_TIMEOUT = 60          # seconds; unlock+sync is seconds, not milliseconds
_SESSION_RE = re.compile(r"^[A-Za-z0-9+/=_-]{40,200}$")

# A .cmd/.bat shim re-parses its arguments, so these characters in an item name become injection.
_SHIM_UNSAFE = set('"%&|<>^()!\r\n')


# ==============================================================================================
# logical key -> vault item (the FIELD is derived, see field_for)
# ==============================================================================================
# This mapping is TOPOLOGY, NOT A SECRET: keeping it in code (overridable per key by
# BW_ITEM_<KEY>) is what makes a failure readable — "item 'aixii-postgres' not found" instead of
# "something went wrong".
#
# Credentials that belong together share one login item and use its native username/password
# fields, which is exactly what a login item is for and what `bw get` addresses directly.

_ITEM_MAP: dict[str, str] = {
    "DB_USER":                     "aixii-postgres",
    "DB_PASSWORD":                 "aixii-postgres",
    "REDIS_USER":                  "aixii-redis",
    "REDIS_USER_PASSWORD":         "aixii-redis",
    "SERVICE_TOKEN":               "aixii-core-api-service-token",
    "FILE_PROCESSOR_TOKEN":        "aixii-file-processor-token",
    "AEROAPI_KEY":                 "aixii-flightaware-aeroapi",
    "AVIATION_EDGE_API_KEY":       "aixii-aviationedge",
    "AVIATION_EDGE_EXTRA_API_KEY": "aixii-aviationedge-extra",
}

# Keys with no built-in item, which a host turns into vault keys by naming the item in its
# environment (`BW_ITEM_MS_WEBHOOK_SECRET=ms_graph_webhook`). They have no default here because the
# item may not exist in a given vault at all, and a default that names a missing item would fail
# every boot on the hosts that keep the value in the environment.

MANAGED_KEYS = tuple(_ITEM_MAP)

# GROUPS: several keys out of ONE item, named once as `BW_ITEM_<GROUP>`. A login item holds a whole
# service principal — id in `username`, secret in `password`, the rest in custom fields — and
# splitting that across four env lines only invites them to drift apart.
#   BW_ITEM_PBIE=powerbi-capacity-spn
# A per-key BW_ITEM_<KEY> still wins, for the odd value that lives somewhere else.
_GROUP_KEYS: dict[str, tuple[str, ...]] = {
    "PBIE": ("PBIE_TENANT_ID", "PBIE_CLIENT_ID", "PBIE_CLIENT_SECRET", "PBIE_SUBSCRIPTION_ID"),
}

# Which FIELD of the item holds a key's value, where it is not the default (see field_for). Only
# custom fields need to be listed: `tennant_id` is spelled the way the vault item spells it.
_FIELD_MAP: dict[str, str] = {
    "PBIE_CLIENT_ID":       "username",
    "PBIE_TENANT_ID":       "tennant_id",
    "PBIE_SUBSCRIPTION_ID": "subscription_id",
}

# The item objects `bw get <object> <item>` knows. Anything else is a CUSTOM field and is read out of
# the item's JSON instead (see _custom_field) — asked for one directly, the CLI answers
# `Unknown object "tennant_id"`.
_NATIVE_OBJECTS = frozenset({"username", "password", "uri", "totp", "notes"})

# Keys that gate an OPTIONAL feature rather than a credential the service needs to work. For these
# — and only these — a vault that HAS a mapping but no such item or field means "the feature is not
# configured here", not "the deployment is broken": the resolver falls back to the environment (with
# a warning) and then to the caller's default, and check_secrets reports them separately instead of
# as a failure. The PBIE keys only unlock the /capacity endpoints, which answer 503 without them.
_OPTIONAL_KEYS = frozenset(_GROUP_KEYS["PBIE"])

# What core-api itself cannot start without — the same list entrypoint.sh checks. Everything else
# managed here belongs to somebody else's process (the _admin loaders, the worker services), so it
# must never be able to fail this service's boot.
BOOT_KEYS = ("DB_USER", "DB_PASSWORD", "REDIS_USER", "REDIS_USER_PASSWORD",
             "SERVICE_TOKEN", "FILE_PROCESSOR_TOKEN", "MS_WEBHOOK_SECRET")


def item_for(key: str) -> tuple[str, str]:
    """Resolve the (item, field) pair for a logical key, honouring the env overrides.

    Returns an EMPTY item name for a key with no mapping at all — the callers read that as "this is
    a plain environment variable on this host", which is what an unmapped key is."""
    return _vault_item(key), field_for(key)


def _vault_item(key: str) -> str:
    """The item a key lives in: its own `BW_ITEM_<KEY>`, else its group's `BW_ITEM_<GROUP>`,
    else the built-in mapping, else nothing."""
    own = os.getenv(f"BW_ITEM_{key}")
    if own:
        return own
    for group, keys in _GROUP_KEYS.items():
        if key in keys:
            shared = os.getenv(f"BW_ITEM_{group}")
            if shared:
                return shared
    return _ITEM_MAP.get(key, "")


def field_for(key: str) -> str:
    """The item's field a key reads, derived from the key itself so the environment does not have to
    repeat it: a `…_USER` key is the item's `username`, anything else its `password`.

    `BW_FIELD_<KEY>` is therefore only for a value kept in a CUSTOM field — a login item that holds a
    whole service principal, say, with the tenant id beside the credentials."""
    override = os.getenv(f"BW_FIELD_{key}")
    if override:
        return override
    if key in _FIELD_MAP:
        return _FIELD_MAP[key]
    return "username" if key.endswith("_USER") else "password"


def declared_keys() -> tuple[str, ...]:
    """Every key that HAS a vault mapping on this host: the built-in ones, any key the environment
    declares with `BW_ITEM_<KEY>`, and the members of any group it names with `BW_ITEM_<GROUP>`.

    A deployment can therefore put a secret this code does not know about into the vault without a
    code change — the item name is topology and belongs in the environment, next to the host's other
    topology. `BW_FIELD_<KEY>` alone does not declare a key: without an item name there is nothing
    to look up."""
    declared: list[str] = []
    for name, value in os.environ.items():
        if not name.startswith("BW_ITEM_") or not value:
            continue
        suffix = name[len("BW_ITEM_"):]
        if not suffix:
            continue
        declared.extend(_GROUP_KEYS.get(suffix, (suffix,)))
    return tuple(dict.fromkeys([*_ITEM_MAP, *sorted(declared)]))


# ==============================================================================================
# errors — classified, so an operator knows WHOSE problem it is
# ==============================================================================================

class SecretsError(RuntimeError):
    """Base class. Every failure path raises one of these; nothing ever falls back to a default."""


class SecretsNotInstalled(SecretsError):
    """The `bw` binary is missing from PATH / BW_CLI_PATH."""


class SecretsUnreachable(SecretsError):
    """Network problem or Vaultwarden is down."""


class SecretsTlsError(SecretsError):
    """Certificate not trusted — set NODE_EXTRA_CA_CERTS to the private CA."""


class SecretsAuthError(SecretsError):
    """Wrong BW_CLIENTID / BW_CLIENTSECRET."""


class SecretsUnlockError(SecretsError):
    """Wrong master password, or `unlock` produced no usable session."""


class SecretsLocked(SecretsError):
    """The session expired. Recoverable: clear it and retry ONCE."""


class SecretsItemNotFound(SecretsError):
    """Wrong or ambiguous item name, or the field is empty."""


class SecretsCliError(SecretsError):
    """Anything else; carries a scrubbed CLI snippet."""


_LOCKED_MARKERS = ("vault is locked", "not logged in", "mac failed", "session key")
_UNREACHABLE_MARKERS = ("getaddrinfo", "enotfound", "econnrefused", "etimedout",
                        "econnreset", "502", "503", "socket hang up")
_TLS_MARKERS = ("self_signed", "self signed", "unable_to_get_issuer", "cert_has_expired",
                "err_tls", "altname", "unable to verify")
_NOT_FOUND_MARKERS = ("not found", "more than one result", "no such")


def _classify(stderr: str, *, stage: str) -> SecretsError:
    low = stderr.lower()
    if any(m in low for m in _TLS_MARKERS):
        return SecretsTlsError(
            f"{stage}: TLS trust failure talking to Vaultwarden — point NODE_EXTRA_CA_CERTS at the "
            f"private CA. {stderr}")
    if any(m in low for m in _UNREACHABLE_MARKERS):
        return SecretsUnreachable(f"{stage}: Vaultwarden unreachable. {stderr}")
    if any(m in low for m in _LOCKED_MARKERS):
        return SecretsLocked(f"{stage}: vault locked / session expired. {stderr}")
    if stage == "login" and any(m in low for m in ("api key", "invalid", "incorrect")):
        return SecretsAuthError(f"{stage}: API key rejected — check BW_CLIENTID/BW_CLIENTSECRET. "
                                f"{stderr}")
    if any(m in low for m in _NOT_FOUND_MARKERS):
        return SecretsItemNotFound(f"{stage}: {stderr}")
    return SecretsCliError(f"{stage}: {stderr}")


# ==============================================================================================
# leak protection
# ==============================================================================================

class Secret(str):
    """A string whose repr is `***`.

    Accidental interpolation into a log line is the most common way a secret escapes, and this makes
    it structurally impossible for the values held INSIDE this module (master password, client
    secret, session key). `require()` deliberately returns a plain `str`: the consumers are pydantic
    settings fields and third-party clients that type-check or serialise their inputs, and a subclass
    that lies about its own repr causes worse bugs there than it prevents.
    """
    __slots__ = ()

    def __repr__(self) -> str:      # noqa: D105
        return "'***'"

    def __str__(self) -> str:       # noqa: D105
        return "***"

    def reveal(self) -> str:
        """Explicit, greppable unwrap. If you are calling this in a log statement, stop."""
        return str.__str__(self)


class _Scrubber:
    """Replaces known secret values with `***` in anything on its way to a log or an exception.

    `bw` echoes back some of what it was given, so an error message is a real leak path. The cap is
    generous on purpose: a 200-character cap once hid the operative half of a CLI crash message.
    """
    _CAP = 2000

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = threading.Lock()

    def watch(self, value: Optional[str]) -> None:
        if value and len(value) >= 4:
            with self._lock:
                self._values.add(str.__str__(value) if isinstance(value, Secret) else value)

    def __call__(self, text: str) -> str:
        if not text:
            return ""
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for value in values:
            text = text.replace(value, "***")
        text = text.strip()
        return text if len(text) <= self._CAP else text[:self._CAP] + " …[truncated]"


# ==============================================================================================
# providers
# ==============================================================================================

class SecretsProvider:
    """Callers only ever see require()/optional(). They must not know where the value came from."""

    name = "base"

    def require(self, key: str) -> str:
        raise NotImplementedError

    def optional(self, key: str, default: str = "") -> str:
        raise NotImplementedError

    def close(self) -> None:
        pass


class EnvSecretsProvider(SecretsProvider):
    """Plain environment. The default, and the path local dev / CI / incident response rely on."""

    name = "env"

    def require(self, key: str) -> str:
        value = os.getenv(key)
        if value is None or value == "":
            raise SecretsItemNotFound(f"{key} is required but not set in the environment")
        return value

    def optional(self, key: str, default: str = "") -> str:
        value = os.getenv(key)
        return default if value is None or value == "" else value


class VaultwardenSecretsProvider(SecretsProvider):
    """Resolves secrets through the Bitwarden CLI against a self-hosted Vaultwarden.

    Unlocks at most once per process, lazily, and caches values in memory only. Every failure raises
    a classified error — there is no path that returns an empty string or a stale value.
    """

    name = "vaultwarden"

    def __init__(self, *, scope: str = "default") -> None:
        self._server = (os.getenv("BW_SERVER") or "").rstrip("/")
        if not self._server:
            raise SecretsError("BW_SERVER is required when SECRETS_BACKEND=vaultwarden")
        self._client_id = os.getenv("BW_CLIENTID") or ""
        self._client_secret = Secret(os.getenv("BW_CLIENTSECRET") or "")
        self._password = Secret(os.getenv("BW_PASSWORD") or "")
        if not (self._client_id and self._client_secret and self._password):
            raise SecretsError(
                "BW_CLIENTID, BW_CLIENTSECRET and BW_PASSWORD are all required when "
                "SECRETS_BACKEND=vaultwarden (the bootstrap secrets)")

        self._scrub = _Scrubber()
        self._scrub.watch(self._client_secret)
        self._scrub.watch(self._password)

        self._cli = self._resolve_cli()
        # One state dir PER concurrent scope: bw keeps login state, the session and an encrypted
        # vault cache in there, and two processes sharing it corrupt each other. Persisted across
        # restarts so a restart skips the login round-trip.
        base = Path(os.getenv("BW_APPDATA_BASE") or (Path.home() / ".aixii-bw"))
        self._appdata = base / f"bw-{scope}"
        self._appdata.mkdir(parents=True, exist_ok=True)

        self._session: Optional[Secret] = None
        self._cache: dict[str, str] = {}
        self._snapshot: Optional[dict[str, list[dict]]] = None   # the vault, read once; see _items
        self._lock = threading.RLock()

    # --- process plumbing ---------------------------------------------------------------------

    @staticmethod
    def _resolve_cli() -> str:
        explicit = os.getenv("BW_CLI_PATH")
        if explicit:
            if not Path(explicit).exists():
                raise SecretsNotInstalled(f"BW_CLI_PATH points at a missing file: {explicit}")
            return explicit
        found = shutil.which("bw")
        if not found:
            raise SecretsNotInstalled(
                "the Bitwarden CLI (`bw`) is not on PATH — install a PINNED release and/or set "
                "BW_CLI_PATH")
        return found

    def _is_shim(self) -> bool:
        return self._cli.lower().endswith((".cmd", ".bat"))

    def _check_arg(self, value: str) -> str:
        """A Windows shim re-parses its arguments; reject rather than pass through."""
        if self._is_shim() and (set(value) & _SHIM_UNSAFE):
            raise SecretsError(
                f"refusing to pass {value!r} to a .cmd/.bat shim — it would be re-parsed. Point "
                f"BW_CLI_PATH at a native executable, or rename the vault item.")
        return value

    def _child_env(self, extra: Optional[dict] = None) -> dict:
        """Start from the real environment, STRIP everything managed, then add only what this call
        needs. Nothing inherits a credential it has no use for."""
        env = {k: v for k, v in os.environ.items()
               if k not in _BOOTSTRAP_VARS and k not in _DANGEROUS_VARS}
        env["BITWARDENCLI_APPDATA_DIR"] = str(self._appdata)
        if extra:
            env.update({k: str.__str__(v) if isinstance(v, Secret) else v
                        for k, v in extra.items()})
        return env

    def _run(self, args: list[str], *, stage: str, env_extra: Optional[dict] = None) -> str:
        """One `bw` invocation. Secrets go in through env_extra, never through args."""
        cmd = [self._cli] + [self._check_arg(a) for a in args]
        popen_kwargs: dict = {}
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True        # own process group -> killable as a tree
        else:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,   # any unexpected prompt gets EOF and fails fast
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._child_env(env_extra),
                text=True,
                encoding="utf-8",
                errors="replace",
                **popen_kwargs,
            )
        except FileNotFoundError as ex:
            raise SecretsNotInstalled(f"cannot spawn `bw` at {self._cli}: {ex}") from None
        except OSError as ex:
            raise SecretsCliError(f"{stage}: cannot spawn `bw`: {self._scrub(str(ex))}") from None

        try:
            stdout, stderr = process.communicate(timeout=_CALL_TIMEOUT)
        except subprocess.TimeoutExpired:
            self._kill_tree(process)
            process.communicate()
            raise SecretsUnreachable(
                f"{stage}: `bw` timed out after {_CALL_TIMEOUT}s — Vaultwarden unreachable or hung"
            ) from None

        if process.returncode != 0:
            raise _classify(self._scrub(stderr or stdout), stage=stage)
        return stdout

    @staticmethod
    def _kill_tree(process: subprocess.Popen) -> None:
        """Collect the descendants FIRST: after a forced kill the children are re-parented and
        become unfindable, which is how `bw` processes leak on a hung vault."""
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
        except Exception:                                    # noqa: BLE001 - best effort
            pass
        try:
            process.kill()
        except Exception:                                    # noqa: BLE001
            pass

    # --- lifecycle ----------------------------------------------------------------------------

    def _status(self) -> dict:
        import json
        raw = self._run(["status"], stage="status")
        try:
            # some CLI builds print a warning line before the JSON
            start = raw.index("{")
            return json.loads(raw[start:])
        except (ValueError, json.JSONDecodeError):
            raise SecretsCliError(f"status: unparseable output: {self._scrub(raw)}") from None

    def _ensure_server(self, status: dict) -> dict:
        """A fresh/isolated state dir reports serverUrl=null. Treat null as "differs" and configure
        anyway — otherwise `bw login` silently targets the bitwarden.com CLOUD and fails with a
        confusing auth error against credentials that are perfectly valid for your own server."""
        current = (status.get("serverUrl") or "").rstrip("/")
        if current == self._server:
            return status
        if status.get("status") not in (None, "unauthenticated"):
            # `config server` is ignored or errors while logged in
            try:
                self._run(["logout"], stage="logout")
            except SecretsError:
                pass                                          # already logged out / does not matter
        self._run(["config", "server", self._server], stage="config-server")
        return self._status()

    def _unlock(self) -> Secret:
        with self._lock:
            if self._session is not None:
                return self._session
            status = self._ensure_server(self._status())

            if status.get("status") == "unauthenticated":
                self._run(["login", "--apikey"], stage="login", env_extra={
                    "BW_CLIENTID": self._client_id,
                    "BW_CLIENTSECRET": self._client_secret,
                })

            # --passwordenv takes the NAME of an env var, not the value: this is the mechanism that
            # keeps the master password out of argv. Never `bw unlock <password>`.
            raw = self._run(["unlock", "--passwordenv", "BW_PASSWORD", "--raw"],
                            stage="unlock", env_extra={"BW_PASSWORD": self._password})
            session = (raw or "").strip()
            if not _SESSION_RE.match(session):
                # a prompt or warning on stdout would otherwise be cached AS the session, and every
                # later call would fail obscurely
                raise SecretsUnlockError(
                    "unlock: no usable session key returned — check BW_PASSWORD "
                    f"(got {len(session)} chars)")
            self._session = Secret(session)
            self._scrub.watch(self._session)

            try:
                # without an explicit sync, `get` can happily read a stale local cache
                self._run(["sync"], stage="sync", env_extra={"BW_SESSION": self._session})
            except SecretsError:
                self._session = None                          # roll back: never half-unlocked
                raise
            _log().info("vault unlocked and synced (server=%s, state=%s)",
                        self._server, self._appdata)
            return self._session

    # --- the interface callers actually use -----------------------------------------------------

    def require(self, key: str) -> str:
        item, field = item_for(key)
        if not item:
            # No mapping means this key was never a vault key: it is a plain environment variable
            # (the mapped ones NEVER fall back — that is the invariant this branch does not touch).
            # Naming an item for it in the environment is what moves it into the vault.
            value = os.getenv(key)
            if not value:
                raise SecretsItemNotFound(
                    f"{key} is not set in the environment and has no vault mapping — "
                    f"set BW_ITEM_{key} to the vault item that holds it, or set {key} itself")
            return value
        with self._lock:
            if key in self._cache:
                return self._cache[key]

        try:
            value = self._get(key, item, field)
        except SecretsLocked:
            # sessions expire and a long-lived process must recover without a restart.
            # Retry EXACTLY once, so a genuinely broken vault cannot spin.
            _log().warning("session expired while resolving %s — re-unlocking and retrying once",
                           key)
            with self._lock:
                self._session = None
            value = self._get(key, item, field)

        with self._lock:
            self._cache[key] = value
        self._scrub.watch(value)
        # key NAME and length only: the same rule the check-secrets report follows
        _log().debug("resolved %s from item '%s' (length %d)", key, item, len(value))
        return value

    def _get(self, key: str, item: str, field: str) -> str:
        entry = self._items().get(item.strip().lower())
        if entry is None:
            raise SecretsItemNotFound(f"{key}: no vault item named '{item}'")
        if len(entry) > 1:
            raise SecretsItemNotFound(
                f"{key}: vault item '{item}' is ambiguous — {len(entry)} items share that name")
        value = self._field_of(key, item, field, entry[0])
        if not value:
            raise SecretsItemNotFound(f"{key}: field '{field}' of vault item '{item}' is empty")
        return value

    def _items(self) -> dict[str, list[dict]]:
        """The whole vault, once per process, indexed by item name.

        Every key used to cost its own `bw get`: a Node process start plus a full vault decrypt,
        measured at 4-5 SECONDS each, so six boot credentials took half a minute. One `bw list
        items` costs the same 4-5 seconds for ALL of them, and every field — including the custom
        ones `bw get` cannot address at all — is in that JSON.

        The snapshot holds every secret the bot can see. It stays in memory, is never logged, and
        the process usually resolves all its keys within seconds of taking it."""
        with self._lock:
            if self._snapshot is not None:
                return self._snapshot
        session = self._unlock()
        raw = self._run(["list", "items", "--raw"], stage="list items",
                        env_extra={"BW_SESSION": session})
        try:
            items = json.loads(raw or "[]") or []
        except ValueError:
            raise SecretsCliError("list items: the vault did not come back as JSON") from None
        index: dict[str, list[dict]] = {}
        for it in items:
            name = (it.get("name") or "").strip().lower()
            if name:
                index.setdefault(name, []).append(it)
        with self._lock:
            self._snapshot = index
        _log().info("vault snapshot taken (%d items)", len(items))
        return index

    @staticmethod
    def _field_of(key: str, item: str, field: str, data: dict) -> str:
        """One field of one item: a built-in object, or a custom field matched by name."""
        if field in _NATIVE_OBJECTS:
            login = data.get("login") or {}
            if field == "notes":
                return (data.get("notes") or "").strip()
            if field == "uri":
                uris = login.get("uris") or []
                return ((uris[0] or {}).get("uri") or "").strip() if uris else ""
            return (login.get(field) or "").strip()
        fields = data.get("fields") or []
        wanted = field.strip().lower()
        for f in fields:
            if (f.get("name") or "").strip().lower() == wanted:
                return (f.get("value") or "").strip()
        names = sorted(n for n in ((f.get("name") or "").strip() for f in fields) if n)
        raise SecretsItemNotFound(
            f"{key}: vault item '{item}' has no custom field '{field}'"
            + (f" (it has: {', '.join(names)})" if names else " (it has no custom fields)"))

    def optional(self, key: str, default: str = "") -> str:
        """Under this backend every MAPPED key is required — that is what fail-closed means. A
        deployment that runs a vault is production, where a silently-empty SERVICE_TOKEN (which
        denies every caller) is a misconfiguration, not a valid state.

        The exception is an OPTIONAL key (see _OPTIONAL_KEYS): it gates a feature, not the service,
        so a vault with no such item means the feature is not configured on this host. Only a
        missing/empty ITEM takes that path — an unreachable vault, a bad credential or a TLS error
        still raises, because "the vault is broken" must never look like "the feature is off"."""
        item, _ = item_for(key)
        if not item:
            return os.getenv(key) or default
        if key not in _OPTIONAL_KEYS:
            return self.require(key)
        try:
            return self.require(key)
        except SecretsItemNotFound:
            from_env = os.getenv(key)
            if from_env:
                # migration path: the host still carries the value while the vault item is created
                _log().warning("%s is not in the vault (item '%s') — using the value from the "
                               "environment; move it into the vault to finish the migration",
                               key, item)
                return from_env
            _log().info("%s is not in the vault (item '%s') and not in the environment — the "
                        "feature it gates stays disabled", key, item)
            return default

    def close(self) -> None:
        with self._lock:
            self._cache.clear()
            self._snapshot = None        # drop the decrypted vault with everything else
            if self._session is None:
                return
            self._session = None
        try:
            self._run(["lock"], stage="lock")
            _log().info("vault locked")
        except SecretsError as ex:
            _log().warning("could not lock the vault on shutdown: %s", ex)  # already scrubbed


# ==============================================================================================
# module-level accessor
# ==============================================================================================

_provider: Optional[SecretsProvider] = None
_provider_lock = threading.Lock()


def backend_name() -> str:
    return (os.getenv("SECRETS_BACKEND") or "env").strip().lower()


def create_provider(*, scope: str = "core-api") -> SecretsProvider:
    backend = backend_name()
    if backend in ("env", ""):
        return EnvSecretsProvider()
    if backend == "vaultwarden":
        return VaultwardenSecretsProvider(scope=os.getenv("BW_SCOPE") or scope)
    raise SecretsError(f"unknown SECRETS_BACKEND '{backend}' (expected 'env' or 'vaultwarden')")


def get_provider() -> SecretsProvider:
    """Process-wide provider. Built on first use so a service that never asks for a secret never
    pays the CLI round-trip — and so an unused key can never fail a boot."""
    global _provider
    if _provider is None:
        with _provider_lock:
            if _provider is None:
                _provider = create_provider()
    return _provider


def require_secret(key: str) -> str:
    """Resolve a secret. Raises a classified SecretsError; never returns an empty string."""
    return get_provider().require(key)


def optional_secret(key: str, default: str = "") -> str:
    """Resolve a secret the service can legitimately run without (env backend only — see
    VaultwardenSecretsProvider.optional)."""
    return get_provider().optional(key, default)


def hand_to_child_processes() -> int:
    """Resolve every declared key ONCE here and pass the values to this process's children through
    the environment, with the backend switched to `env` so they never open the vault themselves.
    Returns how many keys were handed over (0 under the `env` backend, where there is nothing to do).

    Why this exists. uvicorn SPAWNS its workers, so each one re-imports the app and would resolve
    the same secrets again: N more unlock+sync round trips, and — the part that actually broke a
    production boot — N `bw` processes on ONE CLI state directory, which corrupt each other. The
    symptom is a `bw get` that exits 0 with empty output, i.e. a perfectly good credential reported
    as an empty field, in a different worker each time.

    What it costs. The values sit in the worker processes' environment. In this deployment that does
    not widen anything: the vault's own bootstrap credentials (BW_CLIENTID / BW_CLIENTSECRET /
    BW_PASSWORD) are already there, injected by compose, and they open the whole vault. Read the
    environment of one of these processes and the vault was yours either way.

    Call it in the PARENT, before the workers are spawned. The vault is locked afterwards."""
    provider = get_provider()
    if provider.name == "env":
        return 0
    handed = 0
    for key in declared_keys():
        try:
            os.environ[key] = provider.require(key)
            handed += 1
        except SecretsItemNotFound:
            if key in BOOT_KEYS:
                raise      # the workers cannot start without it; fail here, once, with the reason
            # Optional, or resolved by somebody else entirely (the data-provider keys belong to the
            # loaders and the worker services): leave the environment untouched.
            _log().info("%s not handed to the workers — not in the vault on this host", key)
    os.environ["SECRETS_BACKEND"] = "env"
    close_provider()
    _log().info("resolved %d secrets for the worker processes; vault closed in this process", handed)
    return handed


def close_provider() -> None:
    """Lock the vault. Called from the app lifespan on shutdown."""
    global _provider
    with _provider_lock:
        if _provider is not None:
            _provider.close()
            _provider = None


def check_secrets(keys: Optional[list[str]] = None, stream=sys.stdout) -> int:
    """Resolve every managed key and report `KEY -> OK (length N)`. No values, no side effects.

    This turns "the deploy is broken" into a ten-second answer and is the first thing to run on a
    new host. Returns the number of failures.

    Exactly three things are ever emitted, and none of them is a secret value: the key NAME, the
    LENGTH of the resolved value, and the classified failure reason. The length is deliberate — it
    is what catches a truncated or mangled credential (a password containing `$` passed through
    Docker Compose interpolation, say) without disclosing the value. This function owns the whole
    report, including the summary line, so that callers never have to interpolate anything derived
    from a secret in order to print a result.
    """
    provider = get_provider()
    failures = 0
    print(f"backend: {provider.name}", file=stream)
    for key in (keys or declared_keys()):
        try:
            value = provider.require(key)
        except SecretsItemNotFound as ex:
            if key in _OPTIONAL_KEYS and not keys:
                # gates a feature, not the service: not an error unless it was asked for by name
                print(f"  {key} -> not configured (optional) {ex}", file=stream)
                continue
            failures += 1
            print(f"  {key} -> FAIL ({type(ex).__name__}) {ex}", file=stream)
        except SecretsError as ex:
            failures += 1
            # the message is already scrubbed at raise time; it names the item, never the value
            print(f"  {key} -> FAIL ({type(ex).__name__}) {ex}", file=stream)
        else:
            print(f"  {key} -> OK (length {len(value)})", file=stream)
    print(f"\n{'all keys resolved' if not failures else f'{failures} key(s) FAILED'}", file=stream)
    return failures


__all__ = [
    "MANAGED_KEYS", "Secret", "SecretsProvider", "EnvSecretsProvider",
    "VaultwardenSecretsProvider", "SecretsError", "SecretsNotInstalled", "SecretsUnreachable",
    "SecretsTlsError", "SecretsAuthError", "SecretsUnlockError", "SecretsLocked",
    "SecretsItemNotFound", "SecretsCliError",
    "backend_name", "create_provider", "get_provider", "require_secret", "optional_secret",
    "close_provider", "check_secrets", "item_for", "declared_keys",
]
