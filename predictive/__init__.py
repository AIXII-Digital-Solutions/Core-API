"""Airline activity forecasting engine — calibration & diagnostics (standalone).

See ``docs/airline forecast handoff prompt.md`` for the frozen architecture brief.
Build order: Archetype -> Anchor -> Pooling -> Validation. This package implements
the Archetype layer (Step 1, brief sec. 7-8): the diagnostic that decides whether
operational archetypes are discrete modes or a continuum.
"""
