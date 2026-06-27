#!/usr/bin/env python
"""
Per-database Alembic runner.

`alembic -x db=<name>` only selects target_metadata + the DB URL — it does NOT switch
the revision directory (alembic.ini ships ONE active `version_locations`, which only
works for one DB; env.py's per-branch `versions_dir` is informational). Running bare
`alembic -x db=main upgrade head` would therefore apply the WRONG revision tree to the
main DB. Use THIS instead — it sets `version_locations` per DB before Alembic builds the
ScriptDirectory, so each `-x db=` targets the correct tree.

    python tools/migrate.py upgrade main head
    python tools/migrate.py current service
    python tools/migrate.py revision main "add X"     # --autogenerate
    python tools/migrate.py downgrade service -1
"""
import argparse
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]

DB_VERSIONS = {
    "main": "versionsMain",
    "service": "versionsService",
    "cirium": "versionsCirium",
    "airlabs": "versionsAirlabs",
    "flightradar": "versionsFlightRadar",
    "fr": "versionsFlightRadar",
    "aviationedge": "versionsAviationEdge",
    "ae": "versionsAviationEdge",
}


def _config(db: str) -> Config:
    if db not in DB_VERSIONS:
        raise SystemExit(f"unknown db '{db}'. known: {sorted(DB_VERSIONS)}")
    cfg = Config(str(ROOT / "alembic.ini"))
    # set BEFORE any command builds the ScriptDirectory -> correct per-DB revision tree
    cfg.set_main_option("version_locations", str(ROOT / "migration" / DB_VERSIONS[db]))
    # provide `-x db=<name>` to env.py (which reads context.get_x_argument)
    cfg.cmd_opts = argparse.Namespace(x=[f"db={db}"])
    return cfg


def main() -> None:
    args = sys.argv[1:]
    if len(args) < 2:
        raise SystemExit(
            "usage: migrate.py <upgrade|downgrade|current|history|revision|stamp> <db> [rev|message]"
        )
    action, db = args[0], args[1]
    rest = args[2:]
    cfg = _config(db)

    if action == "upgrade":
        command.upgrade(cfg, rest[0] if rest else "head")
    elif action == "downgrade":
        command.downgrade(cfg, rest[0] if rest else "-1")
    elif action == "current":
        command.current(cfg)
    elif action == "history":
        command.history(cfg)
    elif action == "stamp":
        command.stamp(cfg, rest[0] if rest else "head")
    elif action == "revision":
        command.revision(cfg, message=(rest[0] if rest else None), autogenerate=True)
    else:
        raise SystemExit(f"unknown action '{action}'")


if __name__ == "__main__":
    main()
