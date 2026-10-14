"""v1.7.1: ISO language codes and placeholders never reach the database.

Live incident: the dashboard showed 'ta' AND 'Tamil', 'mr' AND 'Marathi',
'my' AND 'Burmese' as separate languages, plus a literal 'No Language' row.
TMDB returns original_language as a bare code when the code is missing from
the app's name map, while spoken_languages carries the full name for the same
film -- the old code stored both.

Run: python tests/test_v171_language_names.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("WATCHLISTARR_DATA", tempfile.mkdtemp(prefix="wlarr-v171-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}{(': ' + extra) if extra else ''}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


from app.metadata import providers as P  # noqa: E402

print("\n1. code + name pairs collapse to the name")
m = P._merge_language_forms(["ta", "Tamil", "mr", "Marathi", "my", "Burmese"])
check("codes resolved and deduped", m == ["Tamil", "Marathi", "Burmese"], str(m))

print("\n2. placeholders are dropped")
m = P._merge_language_forms(["No Language", "English", "xx", "unknown"])
check("placeholder and unmappable code removed", m == ["English"], str(m))

print("\n3. every language in the map resolves to a real name")
import re
bad = [c for c in P._TMDB_LANGUAGES if re.fullmatch(r"[a-z]{2,3}", str(P._TMDB_LANGUAGES[c]))]
check("no code-shaped values in the map", not bad, str(bad[:5]))
check("map is complete (180+ entries)", len(P._TMDB_LANGUAGES) >= 180, str(len(P._TMDB_LANGUAGES)))
check("previously-missing codes now map",
      P._TMDB_LANGUAGES.get("ta") == "Tamil" and P._TMDB_LANGUAGES.get("mr") == "Marathi"
      and P._TMDB_LANGUAGES.get("my") == "Burmese")

print("\n4. _tmdb_detail output never contains a bare code or placeholder")
payload = {
    "id": 42, "title": "T", "release_date": "2001-01-01",
    "original_language": "ta",
    "spoken_languages": [
        {"iso_639_1": "ta", "english_name": "Tamil"},
        {"iso_639_1": "en", "english_name": "English"},
    ],
    "genres": [{"id": 18, "name": "Drama"}],
}
class _R:
    status_code = 200
    def json(self):
        return payload
class _C:
    def __init__(self, *a, **k):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def get(self, url, params=None):
        return _R()

_orig_client = P._client
P._client = lambda: _C()
P._cache = P._TTLCache(1)
try:
    meta = P._tmdb_detail(42, "k")
finally:
    P._client = _orig_client
check("meta resolved", meta is not None)
if meta:
    check("languages are names only", meta.languages == ["Tamil", "English"], str(meta.languages))
    codes = [x for x in meta.languages if re.fullmatch(r"[a-z]{2,3}", x)]
    placeholders = [x for x in meta.languages if x.lower() in ("no language", "unknown")]
    check("no bare codes", not codes, str(codes))
    check("no placeholders", not placeholders, str(placeholders))

print(f"\n{'='*58}\n  {PASS} passed, {FAIL} failed\n{'='*58}")
sys.exit(1 if FAIL else 0)
