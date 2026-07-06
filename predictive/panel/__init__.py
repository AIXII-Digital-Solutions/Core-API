"""forecast data-prep panel.

- predictive.panel.build.build_history(operator, ...)  -> assemble forecast.history_1 (steps 5-7)
- predictive.panel.merge.merge_final(...)              -> history_1 + future_1 -> forecast.final_1 (step 8)

Import from the submodules (`from predictive.panel.build import build_history`); this package
__init__ stays import-free so `python -m predictive.panel.build` doesn't double-import.
See predictive/panel/README.md and docs/airline forecast handoff prompt.md.
"""
