"""Resolve a typed airline name to the reference spelling in cirium.airlines.

The claims sheets are hand-made, so the same carrier arrives as "Corendon Airlines Europe", with a
letter dropped, or with two letters swapped. Storing those verbatim splits one airline across several
rows and quietly breaks every per-airline total downstream. This resolver is the single source of
truth for the spelling that gets stored: the same reference the /airlines typeahead searches, plus
fuzzy matching so a one-character mistake still lands on the right carrier.

WHY TWO SIGNALS. Trigram similarity handles insertions, deletions and substitutions well, but it
collapses on TRANSPOSITIONS — measured against the live reference, "Emirtaes" scores 0.385 on
"Emirates", below any threshold that still rejects junk, while its Levenshtein distance is 2. So a
candidate is accepted on EITHER a high enough similarity OR a small enough edit distance.

WHY A MARGIN, NOT JUST A THRESHOLD. A threshold alone cannot tell a typo from a word that is merely
generic. Measured on the reference: "Airlines" scores 0.818 against "R Airlines" and "Air" scores
0.667 against "J-Air" — both comfortably over any workable threshold. What separates them from a
real typo is that they are equally close to MANY names: their top two candidates score identically
(gap 0.000), while every genuine misspelling in the sample beat its runner-up by at least 0.135. So
an ambiguous match is REPORTED, never guessed at.

WHY A MINIMUM LENGTH. "A" matches the reference row "A+" with similarity 1.000 and a clear margin —
neither rule above catches it. Two characters cannot identify a carrier, so they are refused outright.

Nothing here writes a name the caller did not almost certainly mean: what cannot be resolved
confidently comes back as a rejection listing the near misses, for a human to settle.
"""
from dataclasses import dataclass
from typing import Optional, List, Sequence

from sqlalchemy import text

from Config import setup_logger

logger = setup_logger("airline_resolver")

# Minimum trigram similarity for the best candidate to be considered a match at all. 0.45 sits above
# the junk measured on the reference ("Totally Made Up Air" -> 0.304) and below every real typo in
# the sample (worst: 0.706), leaving transpositions to the edit-distance rule below.
_MIN_SIMILARITY = 0.45

# ...or a Levenshtein distance this small, which is what catches transpositions and single-character
# slips that trigrams under-score. Every genuine misspelling measured came in at 1 or 2.
_MAX_EDIT_DISTANCE = 2

# The best candidate must beat the runner-up by this much. Real typos led by >= 0.135; generic words
# ("Air", "Airlines") led by exactly 0.000 because they are equidistant from dozens of names.
_MIN_MARGIN = 0.05

# Below this many characters a name cannot identify a carrier, whatever it scores.
_MIN_QUERY_LENGTH = 3

# How many candidates to pull back: enough to judge the margin and to show the caller near misses.
_CANDIDATE_LIMIT = 5

# pg_trgm's cutoff for the `%` operator, set per-statement. Deliberately BELOW _MIN_SIMILARITY so a
# transposition (0.385) still reaches the candidate list and can be rescued by edit distance.
_TRGM_FLOOR = 0.25


@dataclass(frozen=True)
class AirlineMatch:
    """The outcome for one typed name.

    `resolved` is the reference spelling to store; None when nothing could be settled confidently.
    `changed` says whether it differs from what was typed — that is what a caller reports back so a
    silent correction is never invisible. `candidates` carries the near misses behind a rejection."""
    typed: str
    resolved: Optional[str]
    exact: bool
    similarity: Optional[float]
    candidates: List[str]
    reason: Optional[str] = None

    @property
    def changed(self) -> bool:
        return bool(self.resolved) and self.resolved != self.typed


# Exact match first, case- and whitespace-insensitive: no scoring can beat a name that is simply
# correct, and this is the path almost every row takes. Ordered so that when the reference holds two
# spellings differing only in case (18 such pairs exist), the pick is at least deterministic.
_EXACT_SQL = text("""
    SELECT airline
    FROM cirium.airlines
    WHERE lower(btrim(airline)) = lower(btrim(:q))
    ORDER BY airline
    LIMIT 1
""")

# Candidates by trigram OR small edit distance. The trigram half uses the GIN index; the edit-distance
# half is bounded by length so it cannot turn into a 63k-row levenshtein sweep on every call.
_FUZZY_SQL = text("""
    SELECT airline,
           similarity(airline, :q) AS sim,
           levenshtein(lower(airline), lower(:q)) AS lev
    FROM cirium.airlines
    WHERE airline % :q
       OR (length(airline) BETWEEN length(:q) - :maxlev AND length(:q) + :maxlev
           AND levenshtein(lower(airline), lower(:q)) <= :maxlev)
    ORDER BY sim DESC, lev ASC, length(airline) ASC, airline ASC
    LIMIT :lim
""")


async def resolve_airline(session, typed: str) -> AirlineMatch:
    """Resolve ONE name. `session` is an AsyncSession on the aixii database."""
    q = " ".join((typed or "").split())   # collapse internal whitespace too, not just the ends
    if len(q) < _MIN_QUERY_LENGTH:
        return AirlineMatch(typed=typed, resolved=None, exact=False, similarity=None, candidates=[],
                            reason=f"too short to identify an airline (minimum {_MIN_QUERY_LENGTH} characters)")

    hit = (await session.execute(_EXACT_SQL, {"q": q})).scalar_one_or_none()
    if hit is not None:
        return AirlineMatch(typed=typed, resolved=hit, exact=True, similarity=1.0, candidates=[hit])

    await session.execute(text("SELECT set_limit(:f)"), {"f": _TRGM_FLOOR})
    rows = (await session.execute(_FUZZY_SQL, {
        "q": q, "maxlev": _MAX_EDIT_DISTANCE, "lim": _CANDIDATE_LIMIT})).all()

    if not rows:
        return AirlineMatch(typed=typed, resolved=None, exact=False, similarity=None, candidates=[],
                            reason="no similar airline in the reference data")

    best = rows[0]
    names = [r.airline for r in rows]
    sim = float(best.sim)

    if sim < _MIN_SIMILARITY and best.lev > _MAX_EDIT_DISTANCE:
        return AirlineMatch(typed=typed, resolved=None, exact=False, similarity=sim, candidates=names,
                            reason="no close enough match in the reference data")

    # Ambiguity check against the best DIFFERENTLY-named candidate.
    runner_up = next((r for r in rows[1:] if r.airline != best.airline), None)
    if runner_up is not None and (sim - float(runner_up.sim)) < _MIN_MARGIN:
        return AirlineMatch(
            typed=typed, resolved=None, exact=False, similarity=sim, candidates=names,
            reason=("matches several airlines equally well — name it exactly as the reference does"))

    logger.info("airline resolved: %r -> %r (similarity %.3f, distance %d)",
                q, best.airline, sim, best.lev)
    return AirlineMatch(typed=typed, resolved=best.airline, exact=False, similarity=sim,
                        candidates=names)


async def resolve_airlines(session, typed_names: Sequence[str]) -> dict:
    """Resolve MANY names, one lookup per DISTINCT spelling.

    A bulk load repeats the same handful of carriers over and over (one row per year, per currency,
    per cover section), so resolving per distinct spelling instead of per row turns hundreds of
    lookups into a few. Returns {typed: AirlineMatch}, keyed by the string as it was passed in."""
    out: dict = {}
    for name in typed_names:
        if name in out:
            continue
        out[name] = await resolve_airline(session, name)
    return out


__all__ = ["AirlineMatch", "resolve_airline", "resolve_airlines"]
