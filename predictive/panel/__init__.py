"""forecast data-prep panel.

- predictive.panel.build.build_history(operator, ...)  -> assemble forecast.acys_actuals (steps 5-7)
- predictive.panel.merge.merge_final(...)              -> acys_actuals + acys_forecast -> acys_summary (step 8)

Import from the submodules (`from predictive.panel.build import build_history`); this package
__init__ stays import-free so `python -m predictive.panel.build` doesn't double-import.
See predictive/panel/README.md and docs/airline forecast handoff prompt.md.
"""
