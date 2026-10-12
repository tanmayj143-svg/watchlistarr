"""Priority scoring engine.

Every candidate gets a score made of independent, user-defined *rules*. A rule
matches some attribute of the movie and awards points. Nothing is hard-coded:
the same engine expresses "1960s China this month" and "Sci-Fi this week".

Rule anatomy (see docs/SCORING.md for the full reference):

    id          stable identifier
    kind        attribute family: genre | country | language | year | decade
                | rating | letterboxd_rating | popularity | runtime
                | source_position | capacity | manual
    op          comparison: eq | neq | in | nin | gte | lte | between | contains
    value       the operand (list for in/nin, [lo,hi] for between)
    points      awarded when matched (may be negative to penalise)
    limit       hard gate: when True the movie is rejected if the rule matches
                          (`exclude_if_match` semantics)
    weight      optional multiplier for fine tuning

Scoring is deliberately *additive* and inspectable: `explain()` returns the
per-rule contribution so the UI can show exactly why a movie scored what it did.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .timewin import TimeWindow, resolve_window

NUMERIC_KINDS = {"rating", "letterboxd_rating", "popularity", "runtime", "year",
                 "decade", "source_position"}
LIST_KINDS = {"genre", "country", "language", "source_position"}
CAPACITY_KINDS = {"capacity", "min_free_gb", "max_free_gb"}
PACE_KINDS = {"pace"}  # driven by the per-watchlist Pace setting, not the UI

ALL_KINDS = LIST_KINDS | NUMERIC_KINDS | CAPACITY_KINDS | PACE_KINDS | {"manual"}

# Canonical facet vocabularies. The UI suggests these but accepts anything,
# because providers disagree on spelling ("Sci-Fi" vs "Science Fiction").
GENRES = [
    # mainstream
    "Action", "Adventure", "Animation", "Biography", "Comedy", "Crime",
    "Documentary", "Drama", "Family", "Fantasy", "Film-Noir", "History",
    "Horror", "Music", "Mystery", "Romance", "Sci-Fi", "Thriller", "War",
    "Western",
    # subgenres &amp; niches commonly returned by providers
    "Action Adventure", "Action Drama", "Adventure Comedy", "Alien Film",
    "American Football", "Animal Film", "Apocalypse", "Archaeology",
    "Architecture", "Archive Footage", "Art Film", "Avant-Garde",
    "Biker", "Buxom", "Camp", "Car Movie", "Chinese Animation",
    "Christmas", "Cold War", "College", "Coming-of-Age", "Concert Film",
    "Costume", "Cyberpunk", "Detective", "Disaster", "Dystopian",
    "Eastern", "Environmental", "Erotica", "Espionage", "Exercise",
    "Experimental", "Fairy Tale", "Fantasy Adventure", "Film Festival",
    "Film-Noir Remake", "Flashback", "Folk", "Food", "Gangster",
    "Gothic", "Heist", "Hip Hop", "Historical", "Independent", "Inspirational",
    "Interviews", "Japanese Anime", "Journalism", "Korean Drama",
    "Live Performance", "Martial Arts", "Medical", "Melodrama",
    "Mockumentary", "Monster", "Motorsport", "Nature", "News",
    "Period Drama", "Philosophical", "Political", "Psychological",
    "Reality TV", "Religious", "Road Movie", "Robot", "Romance Comedy",
    "Satire", "School", "Science Fiction Film", "Silent Film", "Slapstick",
    "Sports", "Steampunk", "Superhero", "Surf", "Surgery", "Suspense",
    "Swords", "Television Movie", "Time Travel", "Tragedy", "Travel",
    "True Crime", "Vampire", "Video Game", "Werewolf", "Zombie",
]
GENRE_ALIASES = {
    "science fiction": "Sci-Fi", "scifi": "Sci-Fi", "sf": "Sci-Fi",
    "science fiction film": "Sci-Fi",
    "action & adventure": "Action Adventure",
    "action and adventure": "Action Adventure",
    "adventure comedy": "Adventure Comedy",
    "anime": "Japanese Anime",
    "sport": "Sports", "sports film": "Sports",
    "superhero film": "Superhero",
    "zombie film": "Zombie",
    "period drama": "Period Drama",
    "coming of age": "Coming-of-Age",
    "kids": "Family", "childrens": "Family",
    "mockumentary": "Mockumentary",
    "true story": "Biography", "based on true story": "Biography",
    "war film": "War", "military": "War",
    "detective fiction": "Detective", "whodunit": "Detective",
    "computer animation": "Animation",
    "stop motion": "Animation",
    "science-fiction": "Sci-Fi",
    "film noir": "Film-Noir", "filmnoir": "Film-Noir", "noir": "Film-Noir",
    "sci fi": "Sci-Fi",
    "animated": "Animation", "animation": "Animation", "kids": "Family",
    "documentaries": "Documentary", "docs": "Documentary",
    "biography": "Biography", "biopic": "Biography",
    "romcom": "Romance", "rom-com": "Romance",
    "fantasy": "Fantasy", "scifi": "Sci-Fi",
}
COUNTRIES = [
    "United States", "United Kingdom", "China", "Hong Kong", "Japan",
    "South Korea", "India", "France", "Germany", "Italy", "Spain", "Canada",
    "Australia", "Mexico", "Brazil", "Russia", "Sweden", "Denmark", "Poland",
    "Netherlands", "Ireland", "France", "Taiwan", "Thailand", "Turkey",
]
LANGUAGES = [
    "English", "Mandarin", "Cantonese", "Japanese", "Korean", "French",
    "German", "Spanish", "Italian", "Russian", "Hindi", "Portuguese",
    "Danish", "Swedish", "Polish", "Dutch", "Turkish", "Persian", "Arabic",
    "Latin",
]


def canonical_genre(value: str) -> str:
    low = re.sub(r"\s+", " ", (value or "").strip().lower())
    if not low:
        return ""
    if low in GENRE_ALIASES:
        return GENRE_ALIASES[low]
    for g in GENRES:
        if low == g.lower():
            return g
    return (value or "").strip().title()


@dataclass
class Rule:
    id: str
    kind: str
    op: str = "in"
    value: Any = None
    points: float = 0.0
    limit: bool = False
    weight: float = 1.0
    enabled: bool = True
    # How many values of a multi-value rule (genre/country/language) may each
    # score. A film tagged Drama + Sci-Fi + History earns 3 x points under one
    # rule instead of 1 x. Capped so one heavily-tagged film cannot dominate.
    max_matches: int = 3
    # capacity rules only
    unit: str | None = None      # gb | count
    threshold: float | None = None
    comparator: str = "lt"        # lt | lte | gt | gte | eq | between
    threshold2: float | None = None
    window: str | None = None     # e.g. "month", "week", "day"
    time_field: str | None = None # requested_at | first_seen_at

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Rule":
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (d or {}).items() if k in allowed})

    def to_dict(self) -> dict[str, Any]:
        return {f: getattr(self, f) for f in self.__dataclass_fields__}  # type: ignore[attr-defined]


@dataclass
class ScoreResult:
    score: float
    matched: list[dict[str, Any]] = field(default_factory=list)
    rejected_by: dict[str, Any] | None = None
    eligible: bool = True


class Scorer:
    """Applies a watchlist's rules to one candidate."""

    def __init__(self, rules: list[Rule], *, capacity: Any | None = None,
                 window: TimeWindow | None = None):
        self.rules = [r for r in rules if r.enabled]
        self.capacity = capacity
        self.window = window

    # -- public ---------------------------------------------------------
    def score(self, cand: dict[str, Any], *, now: datetime | None = None) -> ScoreResult:
        """Score a candidate.

        Scoring and eligibility are deliberately independent. A limit rule
        (Pace, disk, storage guard) may block a film from being requested, but
        it must never change how much the film is worth -- otherwise a paused
        queue makes every film look like a 0-score film. So limits are collected
        rather than returned early, and the real score is always reported.
        """
        now = now or datetime.now(timezone.utc)
        total = 0.0
        matched: list[dict[str, Any]] = []
        blocked_by: dict[str, Any] | None = None

        for rule in self.rules:
            hit, points, why = self._evaluate(rule, cand, now)
            if rule.limit:
                if hit and blocked_by is None:
                    blocked_by = {"rule": rule.to_dict(), "reason": why}
                continue
            if hit:
                contribution = points * (rule.weight if rule.weight else 1.0)
                total += contribution
                matched.append(
                    {
                        "rule_id": rule.id,
                        "kind": rule.kind,
                        "why": why,
                        "points": round(contribution, 2),
                    }
                )

        return ScoreResult(
            score=round(total, 2),
            matched=matched,
            rejected_by=blocked_by,
            eligible=blocked_by is None,
        )

    # -- rule dispatch --------------------------------------------------
    def _evaluate(self, rule: Rule, cand: dict[str, Any], now: datetime) -> tuple[bool, float, str]:
        kind = rule.kind
        if kind == "genre":
            return self._list_match(rule, cand.get("genres") or [], "genre")
        if kind == "country":
            return self._list_match(rule, cand.get("countries") or [], "country")
        if kind == "language":
            return self._list_match(rule, cand.get("languages") or [], "language")
        if kind == "year":
            return self._number_match(rule, cand.get("year"), f"year {cand.get('year')}")
        if kind == "decade":
            return self._decade_match(rule, cand.get("year"))
        if kind == "rating":
            return self._number_match(rule, cand.get("imdb_rating"), f"rating {cand.get('imdb_rating')}")
        if kind == "letterboxd_rating":
            # Letterboxd's community average runs 0-5, not 0-10.
            return self._number_match(
                rule,
                cand.get("letterboxd_rating", cand.get("lb_rating")),
                f"Letterboxd rating {cand.get('letterboxd_rating', cand.get('lb_rating'))}",
            )
        if kind == "popularity":
            return self._number_match(rule, cand.get("popularity"), f"popularity {cand.get('popularity')}")
        if kind == "runtime":
            return self._number_match(rule, cand.get("runtime"), f"runtime {cand.get('runtime')}m")
        if kind == "source_position":
            return self._number_match(rule, cand.get("source_position"), f"position {cand.get('source_position')}")
        if kind == "pace":
            return self._pace_match(rule, cand, now)
        if kind == "capacity":
            return self._capacity_match(rule, cand, now)
        if kind == "min_free_gb":
            return self._min_free_match(rule, cand, now)
        if kind == "max_free_gb":
            return self._max_free_match(rule, cand, now)
        if kind == "manual":
            return self._manual_match(rule, cand)
        return False, 0.0, f"unknown rule kind '{kind}'"

    # -- matchers -------------------------------------------------------
    def _list_match(self, rule: Rule, values: list[str], label: str) -> tuple[bool, float, str]:
        """Match a film against a multi-value rule (genre / country / language).

        A film that satisfies several of the rule's values earns points for each
        one. "Drama and Sci-Fi" scores 2 x points while "Drama" alone scores
        1 x, which is what makes overlapping rules additive instead of binary.
        Points are capped at `rule.max_matches` (default 3) so one film that is
        tagged with six genres cannot dominate the whole ranking.
        """
        canon = [_canon_for(rule.kind, v) for v in (values or [])]
        wanted = rule.value
        if isinstance(wanted, str):
            wanted = [wanted]
        wanted_norm = [_canon_for(rule.kind, str(v)) for v in (wanted or [])]

        base = float(rule.points or 0)

        if rule.op in ("in", "any", "eq"):
            # De-duplicate both sides: the film and the rule can canonicalise to
            # the same genre twice (e.g. "Sci-Fi" and "Science Fiction"), which
            # rendered as "matches sci-fi, sci-fi" and double-counted points.
            unique_canon = list(dict.fromkeys(canon))
            unique_wanted = list(dict.fromkeys(wanted_norm))
            hit_set = [w for w in unique_wanted if w in unique_canon]
            hit = bool(hit_set)
            if hit:
                n = min(len(hit_set), rule.max_matches)
                why = f"{label} matches {', '.join(hit_set)} x{n}"
                return hit, base * n, why
            return False, 0.0, f"{label} has none of {', '.join(unique_wanted)}"

        if rule.op in ("nin", "none", "not_in"):
            hit_set = [w for w in wanted_norm if w in canon]
            hit = not hit_set
            return hit, base, (
                f"{label} has none of {', '.join(wanted_norm)}" if hit
                else f"{label} includes {', '.join(hit_set)}"
            )

        if rule.op == "contains":
            hit = any(w and (w in c or c in w) for w in wanted_norm for c in canon)
            return hit, base, f"{label} contains {', '.join(wanted_norm)}"

        return False, 0.0, f"unsupported op '{rule.op}' for {label}"

    def _number_match(self, rule: Rule, actual: Any, label: str) -> tuple[bool, float, str]:
        if actual is None or actual == "":
            return False, 0.0, f"{label} unknown"
        try:
            num = float(actual)
        except (TypeError, ValueError):
            return False, 0.0, f"{label} not numeric"
        return self._compare(num, rule, label)

    @staticmethod
    def _rule_number(rule: Rule) -> float | None:
        """Coerce a rule's target to a float.

        The UI stores numeric rule values from a text input, so a saved rule can
        carry "4" as a STRING. `float("4")` works but `float("")` does not, and a
        blank field must mean "not configured" rather than 0 -- otherwise an empty
        Rating box silently turned into "score everything at >= 0".
        """
        val = rule.value
        if val is None:
            return None
        if isinstance(val, str):
            val = val.strip()
            if not val:
                return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    def _decade_match(self, rule: Rule, year: Any) -> tuple[bool, float, str]:
        if year in (None, ""):
            return False, 0.0, "year unknown"
        try:
            year = int(year)
        except (TypeError, ValueError):
            return False, 0.0, "year not numeric"
        decade = (year // 10) * 10
        wanted = rule.value
        if isinstance(wanted, str):
            wanted = [wanted]
        wanted_norm = sorted({_norm_decade(w) for w in (wanted or []) if str(w).strip()})
        op = rule.op
        if op in ("in", "any", "eq"):
            hit = decade in wanted_norm
        elif op in ("nin", "none", "not_in"):
            hit = decade not in wanted_norm
        elif op == "between":
            lo, hi = _decade_bounds(rule)
            hit = lo <= decade <= hi
        else:
            hit = False
        why = f"decade {decade}s" + (f" in {wanted_norm}" if op in ("in", "any", "eq") else "")
        return hit, float(rule.points or 0), why

    def _compare(self, num: float, rule: Rule, label: str) -> tuple[bool, float, str]:
        op = rule.op
        pts = float(rule.points or 0)
        target = self._rule_number(rule)
        if target is None:
            return False, 0.0, f"rule has no value set for {label}"
        if op in ("gte", "ge", ">="):
            return num >= target, pts, f"{label} >= {target:g}"
        if op in ("lte", "le", "<="):
            return num <= target, pts, f"{label} <= {target:g}"
        if op in ("gt", ">"):
            return num > target, pts, f"{label} > {target:g}"
        if op in ("lt", "<"):
            return num < target, pts, f"{label} < {target:g}"
        if op in ("eq", "=="):
            return abs(num - target) < 1e-9, pts, f"{label} == {target:g}"
        if op == "neq":
            return abs(num - target) >= 1e-9, pts, f"{label} != {target:g}"
        if op == "between":
            try:
                lo = float(rule.value)
                hi = float(rule.threshold2)
            except (TypeError, ValueError):
                return False, 0.0, "between needs two numeric bounds"
            return lo <= num <= hi, pts, f"{lo:g} <= {label} <= {hi:g}"
        return False, 0.0, f"unsupported op '{op}'"

    # -- structural rules ----------------------------------------------
    def _capacity_match(self, rule: Rule, cand: dict[str, Any], now: datetime) -> tuple[bool, float, str]:
        """Disk-space-aware gating: X GB free / X% used / room for N more."""
        cap = self.capacity
        if cap is None or not getattr(cap, "available", False):
            return False, 0.0, "capacity unknown (Radarr not configured)"
        unit = (rule.unit or "gb").lower()
        cmp_op = rule.comparator or "lt"
        th = float(rule.threshold if rule.threshold is not None else 0)
        if unit == "percent":
            actual = getattr(cap, "used_fraction", 0.0) * 100.0
            label = "disk used %"
        elif unit == "free_gb":
            actual = getattr(cap, "free_gb", 0.0)
            label = "free GB"
        elif unit == "used_gb":
            actual = getattr(cap, "used_gb", 0.0)
            label = "used GB"
        elif unit == "movie_count":
            actual = float(getattr(cap, "movie_count", 0))
            label = "library count"
        else:  # gb
            actual = getattr(cap, "free_gb", 0.0)
            label = "free GB"

        if cmp_op == "between" and rule.threshold2 is not None:
            lo, hi = th, float(rule.threshold2)
            hit = lo <= actual <= hi
            why = f"{label} {actual:.1f} in [{lo:g}, {hi:g}]"
        else:
            hit = _compare_count(actual, cmp_op, th)
            why = f"{label} {actual:.1f} {cmp_op} {th:g}"
        return hit, float(rule.points or 0), why

    def _min_free_match(self, rule: Rule, cand: dict[str, Any], now: datetime) -> tuple[bool, float, str]:
        """Global floor: refuse to add while free space is under the threshold."""
        cap = self.capacity
        if cap is None or not getattr(cap, "available", False):
            return False, 0.0, "capacity unknown (Radarr not configured)"
        th = float(rule.threshold if rule.threshold is not None else 0)
        free = getattr(cap, "free_gb", 0.0)
        cmp_op = rule.comparator or "gte"
        # For a LIMIT rule a "match" means BLOCK. The requirement here is
        # "free space >= th", so we block precisely when it is NOT satisfied.
        satisfied = _compare_count(free, cmp_op, th)
        hit = not satisfied
        why = (
            f"storage floor: only {free:.1f} GB free, needs {th:g} GB"
            if hit
            else f"free {free:.1f} GB {cmp_op} {th:g} GB OK"
        )
        return hit, float(rule.points or 0), why

    def _max_free_match(self, rule: Rule, cand: dict[str, Any], now: datetime) -> tuple[bool, float, str]:
            """Global ceiling: keep adding only while the drive is emptier than wanted.

            Blocks when free space is ABOVE the ceiling (disk too empty -> stop
            adding) and also when there is not even room for one more film.
            `threshold2` (est_movie_gb) additionally refuses the last film when
            taking it would push free space back above the ceiling.
            """
            cap = self.capacity
            if cap is None or not getattr(cap, "available", False):
                return False, 0.0, "capacity unknown (Radarr not configured)"
            th = float(rule.threshold if rule.threshold is not None else 0)
            est = float(rule.threshold2 if rule.threshold2 is not None else 0)
            free = getattr(cap, "free_gb", 0.0)

            # Not even one more film fits.
            if est > 0 and free < est:
                return True, 0.0, f"free {free:.1f} GB cannot fit an estimated {est:g} GB film"

            if est > 0 and free <= th:
                # Taking this film would refill past the ceiling -> hold off.
                if (free - est) > th:
                    return (
                        True,
                        0.0,
                        f"free {free:.1f} GB; a {est:g} GB film would leave "
                        f"{free - est:.1f} GB, above the {th:g} GB ceiling",
                    )
                return False, 0.0, f"free {free:.1f} GB, adding brings it to {free - est:.1f} GB"

            if free > th:
                return (
                    True,
                    0.0,
                    f"storage ceiling: {free:.1f} GB free is above the {th:g} GB ceiling, topping up paused",
                )
            return False, 0.0, f"free {free:.1f} GB <= ceiling {th:g} GB"

    def _pace_match(self, rule: Rule, cand: dict[str, Any], now: datetime) -> tuple[bool, float, str]:
        """Pace: "at most N per window" -- the single X-per-Y control.

        A match means BLOCK, so this returns True only once the window is full.
        The UI exposes this as the Pace setting rather than as a rule row, which
        is why it has no entry in the kind dropdown.
        """
        target = float(rule.threshold if rule.threshold is not None else 5)
        name = rule.window or "month"
        per_window = cand.get("_window_counts") or {}
        if name in per_window:
            counted = int(per_window.get(name) or 0)
        else:
            counted = int(cand.get("_window_request_count") or 0)
        window = resolve_window(name, now)
        exhausted = counted >= target
        return exhausted, 0.0, f"pace: {counted:g}/{target:g} requested {window.label}"

    def _manual_match(self, rule: Rule, cand: dict[str, Any]) -> tuple[bool, float, str]:
        flag = bool((cand.get("meta") or {}).get("manual_flags") or {})
        value = str(rule.value or "")
        hit = flag.get(value, False) if isinstance(flag, dict) else False
        return hit, float(rule.points or 0), f"manual flag '{value}'" + (" set" if hit else " not set")


def _compare_count(actual: float, op: str, target: float) -> bool:
    if op in ("lt", "<"):
        return actual < target
    if op in ("lte", "le", "<="):
        return actual <= target
    if op in ("gt", ">"):
        return actual > target
    if op in ("gte", "ge", ">="):
        return actual >= target
    if op in ("eq", "=="):
        return abs(actual - target) < 1e-9
    if op in ("neq", "!="):
        return abs(actual - target) >= 1e-9
    return False


def _canon_for(kind: str, value: str) -> str:
    if kind == "genre":
        return canonical_genre(value).lower()
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def _norm_decade(value: Any) -> int:
    """Accept 1960, "1960", "1960s", "60s" -> 1960."""
    m = re.search(r"(\d{4})", str(value))
    if m:
        return (int(m.group(1)) // 10) * 10
    m = re.search(r"(\d{2})\s*s?$", str(value).strip(), re.I)
    if m:
        return (1900 + int(m.group(1))) if int(m.group(1)) > 30 else (2000 + int(m.group(1)))
    return 0


def _decade_bounds(rule: Rule) -> tuple[int, int]:
    lo = _norm_decade(rule.value)
    hi = _norm_decade(rule.threshold2 if rule.threshold2 is not None else rule.value)
    return (min(lo, hi), max(lo, hi))


def parse_rules(raw: Any) -> list[Rule]:
    if isinstance(raw, dict):
        raw = raw.get("rules") or []
    if not isinstance(raw, list):
        return []
    out: list[Rule] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        item.setdefault("id", f"rule-{i}")
        try:
            out.append(Rule.from_dict(item))
        except TypeError as exc:
            # A malformed rule must not take the whole watchlist offline.
            import logging

            logging.getLogger("watchlistarr.scoring").warning(
                "skipping malformed rule %r: %s", item, exc
            )
    return out


def presets() -> list[dict[str, Any]]:
    """One-click rule sets that cover the brief's example asks."""
    return [
        {
            "id": "1960s-china-month",
            "name": "1960s Chinese cinema this month",
            "pace": {"count": 4, "window": "month"},
            "description": "Max 4/month (set in Pace), heavy points for 1960s + China, Mandarin.",
            "rules": [
                {"id": "dec", "kind": "decade", "op": "in", "value": [1960], "points": 40},
                {"id": "ctry", "kind": "country", "op": "in", "value": ["China", "Hong Kong", "Taiwan"], "points": 30},
                {"id": "lang", "kind": "language", "op": "in", "value": ["Mandarin", "Cantonese"], "points": 20},
                {"id": "rating", "kind": "rating", "op": "gte", "value": 7.5, "points": 10},
                {"id": "new", "kind": "decade", "op": "nin", "value": [1960], "points": -15},
            ],
        },
        {
            "id": "scifi-week",
            "name": "Sci-Fi this week",
            "pace": {"count": 2, "window": "week"},
            "description": "Max 2/week (set in Pace), strong Sci-Fi bias, prefer recent + well rated.",
            "rules": [
                {"id": "sf", "kind": "genre", "op": "in", "value": ["Sci-Fi"], "points": 40},
                {"id": "rating", "kind": "rating", "op": "gte", "value": 7.0, "points": 15},
                {"id": "year", "kind": "year", "op": "gte", "value": 2010, "points": 10},
                {"id": "nonsf", "kind": "genre", "op": "nin", "value": ["Sci-Fi"], "points": -25},
            ],
        },
        {
            "id": "disk-aware",
            "name": "Disk-aware backlog",
            "description": "Pause adding when under 100 GB free; keep going above it.",
            "rules": [
                {"id": "disk", "kind": "capacity", "unit": "free_gb", "comparator": "lt", "threshold": 100, "limit": True, "points": 0},
                {"id": "rated", "kind": "rating", "op": "gte", "value": 7.5, "points": 25},
                {"id": "runtime", "kind": "runtime", "op": "lte", "value": 140, "points": 5},
            ],
        },
        {
            "id": "classics",
            "name": "Classic Hollywood",
            "pace": {"count": 3, "window": "week"},
            "description": "Max 3/week (set in Pace), pre-war to 1969, English-language, highly rated.",
            "rules": [
                {"id": "dec", "kind": "decade", "op": "between", "value": 1930, "threshold2": 1969, "points": 35},
                {"id": "lang", "kind": "language", "op": "in", "value": ["English"], "points": 20},
                {"id": "rating", "kind": "rating", "op": "gte", "value": 8.0, "points": 25},
            ],
        },
    ]


def timedelta_for(window: str) -> timedelta:  # pragma: no cover - re-export shim
    return resolve_window(window).timedelta