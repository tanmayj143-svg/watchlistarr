# Watchlistarr — master document

**Project:** Watchlistarr — ARR-ecosystem watchlist → scored Seerr request bot
**Owner:** Tanmay Jain · built by ARCHIE (Hermes Agent)
**Started:** 2026-10-09
**Current version:** v1.7.2 (`versions/003-v1.7.0`, source of truth)
**Status:** v1.7.2 PRODUCTION — deployed and healthy on `192.168.1.37:7777` (local Docker; 2026-10-14). Single live instance. Deploy protocol: version-bump 4 files → full suite → tar+sha256 in dist/ → deploy live → verify /healthz → remove old images → archive + docs in repo.

---

### v1.7.0 — `versions/003-v1.7.0` (NEW — full design review implemented)

Source: the 2026-10-14 design-and-logic review (`DRAFT_review_v1.6.5.md`,
operator-approved). Full suite: **16 Python suites + 1 JS suite, all passing**
(includes the new `tests/test_v170_min_score_gate.py`, 10 checks).

**1. Zero-score filler requests blocked — the v1.3.1 incident invariant restored**
`tests/test_v170_min_score_gate.py`. The v1.3.1 production bug ("it just
requested the top 5 films I added") was patched with `min_score`, then the
setting was silently removed and `test_ranking.py` asserted a stale value was
*ignored* — so an all-zero queue again auto-requested arbitrary films. v1.7.0
makes the invariant structural in `request_for_watchlist` /
`request_for_watchlist_from_items`: an automatic request requires `score > 0`;
a 0-score queue simply exhausts (candidates stay pending, nothing is marked
error). Manual paths ("Request next", per-row Request) deliberately bypass the
gate. The two test fixtures that depended on 0-score films going out
(`test_pace_network_retry.py`) now carry a real matching rule — the old
fixtures encoded the bug.

**2. Request cycle evaluates candidates once, not twice**
`refresh_scores(..., return_items=True)` returns the evaluated list;
`run_cycle` hands it to the new `request_for_watchlist_from_items`. Previously
`evaluate_watchlist` ran over every candidate twice per request cycle
(once in `refresh_scores`, once again inside `request_for_watchlist`).
`tests/test_v170_min_score_gate.py` asserts the evaluator runs exactly once.

**3. Cinemeta / Wikidata / IMDb-suggestion / country-language-guess DELETED (operator directive)**
Metadata is now TMDB-only and honestly so. **Key finding during review:** the
v1.6.5 keyless path was already dead — `resolve()` returned None without a TMDB
key while all the Cinemeta code still shipped, so keyless enrichment silently
produced blank rows (verified live: `cinemeta_search`/`cinemeta_by_imdb` both
returned full data, `resolve()` still returned None). v1.7.0 deletes
`app/metadata/wikidata.py`, the Cinemeta/IMDb-suggest sections and the
`_COUNTRY_DEFAULT_LANGUAGE` guess table, and rewrites `providers.py` around
TMDB (search validated by title + exact year, `find` by IMDb id, details
endpoint for all names). **A free TMDB key is now REQUIRED**; backfill returns
an explicit "no TMDB key" error instead of hundreds of doomed lookups. The
live deployment already has a TMDB key set (verified in the container DB).
Settings UI and README/docs updated; `test_scoring_v15.py` now asserts the
guess helpers are gone.

**4. Home dashboard "in list" number was always blank — fixed**
`home.html` read `last_sync_stats.get('added')`, a stats key that never exists
(sync writes `fetched`/`new`/`updated`). Cards now show three real counts —
tracked / pending / requested — computed in `list_watchlists()` with one
GROUP BY.

**5. Enrich budget prioritises oldest debt; LB-ratings backlog doubles**
`sync_watchlist` sorts unresolved-existing rows ahead of brand-new rows before
applying the enrich budget, so a large import drains its gaps instead of
starving behind fresh arrivals. The per-sync Letterboxd-ratings cap rose
60 → 150 (backfill remains idempotent; cached slugs skipped), cutting a
400-film rating backlog from ~7 hours to ~3 syncs.

**6. Docs**: `docs/SOURCES.md` rewritten around TMDB-as-single-source (with the
v1.6.5 keyless-amputation history), README updated (TMDB key required, source
table, troubleshooting), Settings page states the requirement and links
themoviedb.org, Sources table drops Cinemeta/Wikidata rows.

**Known limitation (documented, by design):** without a TMDB key no enrichment
happens at all. This is the honest version of v1.6.5's behaviour, which also
produced no enrichment — but logged the reason invisibly.

---

### v1.7.2 — `versions/003-v1.7.0` (Pace → Frequency, operator decision)

**Semantics change (operator-approved 2026-10-14):** "N per window" is no longer
a maximum that blocks once hit — it is a **frequency trigger**. The window is
divided into N equal slots; a request goes out when the previous request inside
the current window is at least one slot old. N=2/month → one request every ~15
days; 4/month → every ~7.5 days; 1/day → daily. Once all N slots are spent it
waits for the next window (the ceiling is unchanged; only the spread is new).

**Implementation**
- `scoring.py`: `_pace_match` → `_frequency_match` (kind `frequency`; the
  historical `pace` kind dispatches to the same logic, so persisted rules keep
  working). Slot arithmetic uses `window.timedelta / N`; the reference time is
  the per-window most-recent request.
- `engine.py`: new `_window_last_requested()` supplies
  `_window_last_requested_at` per window; `evaluate_watchlist` passes it into
  every candidate payload.
- `explain.py`/templates: "Pace limit" → "Frequency limit"; the watchlist
  section is now **Frequency** with slot arithmetic in the help text
  ("4 per month ≈ one every 7.5 days"). Manual Request next still bypasses.
- Docs: README rule table + limits section, `docs/SCORING.md` Frequency section.

**Tests**: new `tests/test_v172_frequency.py` (13 checks: slot arithmetic for
2/month, 1/day, 4/month; score independence; legacy pace dispatch; missing
timestamp fallback). Updated `test_scoring.py` and `test_v11.py` — their old
fixtures encoded the burst-cap behaviour. Full suite: **18 Python + 1 JS, all
passing**. Version bumped in all four files;
`dist/watchlistarr-v1.7.2.tar.gz` packaged.

---

### v1.7.1 — `versions/003-v1.7.0` (language-name hygiene)

**Live-reported:** the dashboard's "Languages requested" showed `ta` AND
`Tamil`, `mr` AND `Marathi`, `my` AND `Burmese` as separate languages, plus a
literal `No Language` row. Root cause: TMDB returns `original_language` as a
bare ISO code when that code is missing from the app's 20-entry name map, while
`spoken_languages` carries the full English name for the same film — the old
code stored both, double-counting each language.

**Fixed at the ingestion boundary** (`app/metadata/providers.py`):
- `_TMDB_LANGUAGES` expanded 20 → **186 entries**, generated from TMDB's own
  `/configuration/languages` endpoint (not hand-picked), so codes resolve to
  names at source.
- New `_merge_language_forms()`: code-shaped values are stored only as their
  resolved name (deduped against the name form), unmappable short codes are
  dropped, and placeholders (`No Language`, `unknown`) never persist.

**Live data repaired** (script run inside the v1.6.5 container): 124 of 808
rows rewritten; post-check confirms **0** code-shaped or placeholder values
remain. Dashboard now reads: Tamil 5, English 3, Mandarin 2, Hindi 2, Marathi 1,
Burmese 1, French 1, Japanese 1.

**Regression suite**: `tests/test_v171_language_names.py` (9 checks) — code+name
collapse, placeholder removal, map completeness, and an end-to-end
`_tmdb_detail` assertion that no bare code or placeholder can reach a stored
row. Full suite: 17 Python + 1 JS, all passing. Version bumped in all four
files; `dist/watchlistarr-v1.7.1.tar.gz` packaged.

---


### v1.6.5 — `192.168.1.37` local Docker container (SUPERSEDED by v1.7.0; live until deploy)

**1. Network error resilience & candidate status preservation** (`tests/test_pace_network_retry.py`)
When Seerr/Radarr are unreachable (e.g. host down, connection refused, No route to host),
`create_request` halts the candidate loop immediately instead of hammering the offline host
and marking all pending candidates with `STATUS_ERROR`. Candidates remain in their pending/discovered
state so they are ready for intake as soon as the target service is reachable.

**2. Automatic background pace retry & initial boot pass** (`tests/test_pace_network_retry.py`)
- APScheduler triggers an initial request cycle 10 seconds post-boot so intake does not wait
  up to 12 hours (`request_frequency_minutes`) for its first pass.
- In `_scheduler_loop`, if any active watchlist has unmet pace quota and eligible candidates
  (such as after a temporary network drop to Seerr), the scheduler automatically triggers a retry
  cycle every 15 minutes without requiring human or external intervention.
- Synchronisation and request intervals are cleanly decoupled in `schedule_sync` and `schedule_request`.

**Test totals:** 332 checks across 14 suites (all passing).

---

### v1.6.4 — `versions/002-v1.1.0`

Reads Letterboxd (and optionally IMDb) watchlists, scores every movie against
user-defined point-based priority rules, and sends the highest-scoring ones to
Seerr as requests. Free data sources only; no paid API; no login to any external
service.

Deployed as a Docker container serving a WebUI on **port 7777**.

---

## 2. Live deployment

| Item | Value |
|---|---|
| Host | GPU VM `192.168.1.35` (user `hermes`) |
| Path | `~/watchlistarr` |
| Container | `watchlistarr` (image `watchlistarr:1.0.0`) |
| URL | http://192.168.1.35:7777 |
| Data volume | `watchlistarr_data` (SQLite + logs) |
| Status | healthy, 0 restarts, 0 errors in log |

```bash
ssh -i ~/.ssh/hermes_vm_key hermes@192.168.1.35
cd ~/watchlistarr && docker compose ps | logs -f | restart
```

### Verified integrations (read-only tests)

| Service | URL | Status |
|---|---|---|
| Seerr | http://192.168.1.21:5055 | `Connected — Seerr v3.3.0` |
| Radarr | http://192.168.1.21:7878 | `Connected — Radarr v6.1.1.10360` |
| Library read | — | 739 movies, 270 with files |

---

## 3. Test results

| Suite | Result | Notes |
|---|---|---|
| `tests/test_scoring.py` | **6/6** | runs inside the deployed image |
| `tests/test_e2e.py` | **53/53** | live Letterboxd + mock Seerr/Radarr |
| `tests/test_frontend.js` | **35/35** | app.js in a DOM; XSS escaping; all rule shapes; picker; 401 guard |
| `tests/test_v11.py` | **27/27** | storage guards, Radarr recognition, genre vocabulary |
| `tests/test_version_consistency.py` | **5/5** | version string cannot drift |
| Deployed-page audit | **21/21** | no 500s, no Jinja leaks, no `undefined` |

### Live proof against real services

1. Letterboxd Top 500 list synced — **30 movies**, 27 with TMDB ids, 25 with
   language, 30 with posters.
2. Rules ranked correctly — *The Godfather* (1972, 9.2) top-scored at **55**;
   modern English-language films at 35.
3. Radarr capacity read live — **486.5 GB free, 739 movies**.
4. Request created in real Seerr — **id 799**, TMDB 278.
5. **Test request deleted** — HTTP 204, then 404. Newest request is back to 798.
   Nothing pre-existing was touched (baseline captured before testing).

---

## 4. Bugs found and fixed during development

Every one of these was caught by executing code, not by reading it.

| # | Bug | Impact | Fix |
|---|---|---|---|
| 1 | `discover_lists` returned `[]` | list discovery broken | `/lists/` uses **relative** hrefs; also filters `/edit/`, `/likes/`, `/comments/` |
| 2 | `year in desc` TypeError in Wikidata scoring | language enrichment silently dead | `str(year) in desc` |
| 3 | Wikidata `P577` value shape | enrichment crashed | accepts both str and dict |
| 4 | `except Exception: pass` hid bug #2 | failures looked like "no data" | failures now logged, never swallowed |
| 5 | Bare `except httpx.HTTPError` hid 429s | outage indistinguishable from empty data | retry with logging |
| 6 | `uvicorn.run(forward_allow_ips=...)` | **container would not start** | removed unsupported kwarg |
| 7 | `app/web/server.py` used `.` imports | **container would not start** | corrected to `..` |
| 8 | Static path `app/web/web/static` | **container would not start** | `BASE_DIR` already includes `web` |
| 9 | Starlette 1.x `TemplateResponse` signature | **every page 500'd** | version-detecting `render()` shim |
| 10 | `Cookie(default=None)` binds to *param name* | **all API calls 401'd** | explicit `alias=` |
| 11 | `posterCard` called from Jinja | **watchlist page 500'd** | rendered the rail in Jinja instead |
| 12 | Serial enrichment | 25-item sync >300 s | thread pool, 8 workers |
| 13 | Wikidata 429s in retry loop | sync stalled ~30 s/item | slot throttle + fast give-up |
| 14 | APScheduler missing | background sync died silently | optional import with warning |
| 15 | **401 → `/login` redirect loop** | **WebUI unusable — infinite refresh** | `api()` refuses to redirect when already on `/login`, guards repeat redirects, and boot skips `loadVocab()` on auth pages |

**Bug #15 made the UI unusable** and was found by the user, not by tests. The
`api()` helper redirected to `/login` on any 401 — including calls made *by* the
login page, which produced `/api → 401 → /login → /api` forever. Any visitor who
wasn't already signed in got stuck. Regression-tested in
`tests/test_frontend.js` (the guard is exercised behaviourally, not just
grepped).

**Bug #11** — the watchlist detail page crashed for any list with movies. Found
only by requesting the deployed page, not by any unit test.

---

## 5. Design decisions

| Decision | Reason |
|---|---|
| Letterboxd HTML, not RSS | RSS is Cloudflare-blocked (403 even with a browser UA); HTML serves fine |
| Cinemeta for metadata | richest keyless source; returns IMDb + TMDB ids, genre, country, rating |
| Wikidata for language only | Cinemeta leaves `language` empty ~90% of the time |
| Country→language heuristic as last resort | keeps language sliders functional during Wikidata 429s; flagged as guessed |
| SQLite, no ORM | tiny image, readable schema, easy backup |
| HMAC-signed cookie sessions | no session store to run; stateless across restarts |
| PBKDF2-SHA256, 260k rounds | passwords never stored in plaintext |
| Secrets masked in API (`••••••••`) | settings page can render without leaking keys |
| No external fonts/CDN | works offline on a LAN; nothing to break upstream |
| Additive scoring | point values stay meaningful and tunable; breakdown shown per row |

---

## 6. Known limitations (honest list)

1. **IMDb lists are usually blocked.** IMDb returns 403/202 to server IPs. The
   adapter ships and fails loudly. Use Letterboxd.
2. **Wikidata is rate-limited.** Anonymous callers get 429s under load. Handled
   with throttling, caching and a fallback — but language data may be incomplete
   during heavy syncs.
3. **First sync is slow** (~90 s for 30 items) because each new movie needs
   several network lookups. Later syncs are fast.
4. **Docker was never run on this build host** — no Docker daemon here. The
   image is built and tested **on the VM**, which is the deployment target.
5. **Radarr reports `/data` as 0 bytes free** of 1.8 TB. Capacity rules read
   `/` (486 GB free) and exclude that mount. Worth checking on your side — it may
   be a Radarr path-mapping issue.
6. **"Already requested" is assumed.** Watchlistarr tracks what *it* requested;
   it cannot see movies already in your library unless Radarr is configured.
7. **One admin account**, no multi-user roles — as specified.

---

## 7. Version history

### v1.0.0 — `versions/001-v1.0.0` (current)

Initial alpha.

**Added**
- Letterboxd source adapter (lists, member watchlists, pagination, discovery)
- IMDb list adapter (experimental)
- Cinemeta + IMDb suggestion + optional TMDB metadata; Wikidata language backfill
- Point-based scoring engine: genre, country, language, decade, year, rating,
  popularity, runtime, source position
- Hard limits: quota per window, disk capacity, library count
- Per-movie score breakdown shown in the UI
- Seerr client (request, status, profiles, root folders); Radarr capacity client
- SQLite persistence with full request/event history; idempotent requesting
- WebUI: dashboard with poster rails and top-facet breakdowns, watchlist detail
  with rule editor, settings page
- Single admin login created on first boot; PBKDF2 hashing; signed sessions
- APScheduler background sync and request cycles
- Dockerfile + docker-compose with persistent volume and healthcheck
- Test suites: scoring (6), end-to-end (53), frontend (15)

**Verified**
- Deployed to VM, healthy, 0 errors
- 74 automated checks passing
- Real Seerr request created and cleanly deleted

---

### v1.1.0 — `versions/002-v1.1.0` (current)

Alpha fixes plus four requested features.

**Added**
- **Global storage guards** (Settings → Storage guards): `min_free_gb` (only add
  above this floor), `max_free_gb` (keep adding only at/below this ceiling, so
  the drive is never left mostly empty), `est_movie_gb` (optional look-ahead so
  the last request cannot refill past the ceiling).
- Same controls available per watchlist as `min_free_gb` / `max_free_gb` rule
  kinds, with quick-add buttons in the rule editor.
- **Ignore films already in Radarr** (on by default). Matches on TMDB id, IMDb
  id, or title+year via normalised titles (so "The Godfather" == "Godfather,
  The"), and treats a Radarr record with `hasFile=false` as *known-but-deleted*
  so removed films are not re-added. Remakes are not confused: a differing year
  prevents a title match.
- **Genre vocabulary expanded 20 → 110**, including niches (Cyberpunk, Heist,
  Road Movie, Zombie, Film-Noir, Samurai/Zombie, Coming-of-Age…) and new aliases
  (`anime` → Japanese Anime, `science fiction film` → Sci-Fi).
- **Searchable picker** replacing the native `<select multiple>` that was
  clipped to an unreadable sliver and spilled its option list outside the rule
  box. Now a button showing "N selected: …", with a search field, tick list and
  clear/done controls, constrained to 320px max height.

**Fixed**
- Limit-rule polarity: `min_free_gb`/`max_free_gb` initially blocked when the
  requirement was *satisfied* — inverted, because for a limit rule a match means
  block. Caught by `test_v11.py` before deploy.
- `/healthz` reported 1.0.0 after a 1.1.0 deploy (hardcoded string). Version is
  now consistent across `config.py`, the compose tag, the Dockerfile label and
  README, asserted by `test_version_consistency.py`.
- Ternary-chain syntax error introduced while adding the picker (double `:`),
  caught by `node --check` + the frontend suite.

**Verified against the live stack**
- Storage guards at real 462.7 GB free: `max_free_gb=900` → 0 blocked (allows);
  `max_free_gb=300` → 20/30 blocked with the ceiling reason; `min_free_gb=1000`
  → 20/30 blocked with the floor reason. Defaults restored afterwards.
- Radarr-exists skip: 7 of 30 candidates auto-skipped by TMDB id against the real
  742-film library (incl. Yi Yi, Ran).
- Test suites: 6 scoring + 27 v1.1 + 35 frontend + 5 version = **73 checks**.
- Requests 800/801 created during user testing were deleted; Seerr baseline (798)
  restored and re-verified.

---

### v1.1.2 — `versions/002-v1.1.0` (current)

Rule-editor fixes, Letterboxd rating ranking, plus a latent quota bug.

**Added**
- **Letterboxd rating rule** (`letterboxd_rating`, 0–5 scale). The community
  average is *not* on list pages (those carry only `data-owner-rating`, which is 0
  for unrated films); it is only in JSON-LD on the film page — verified live
  (`the-thing` → 4.35 from 1,455,715 votes). Fetched concurrently (4 workers),
  rate-limited, cached 7 days in a separate SQLite file, and stored in new
  `candidates.lb_rating` / `lb_votes` columns (forward-migrated via ALTER TABLE).
  Triggered by the **Fetch Letterboxd ratings** button rather than every sync,
  since it costs one request per film. Badges appear in the ranked table.
- **List size quick-add button** — the rule existed in the engine but had no way
  to be added from the UI.

**Fixed**
- **Limit-rule comparator did nothing.** The operator dropdown was `disabled` for
  quota/disk/list-size rules, and a disabled control is skipped by the
  `[data-f]` DOM scan, so the chosen comparison was never serialised — "is at
  least" looked selected but did nothing. Now enabled with limit-appropriate
  labels (`is at least ≥`, `is less than <`, …).
- **`list_size` had no comparator control at all**, so it always fell back to the
  server default.
- **Letterboxd rating cache never hit.** `cached()` parsed the stored ISO-8601
  timestamp with a naive `strptime` format that did not match the `+00:00`
  offset, so every film was re-fetched on every run. Now `datetime.fromisoformat`.
- Stray `import logging as _json_mod` left in `db.py` from an early edit, which
  broke startup once a real `logging` import was added.

**Verified live**
- 30/30 ratings fetched and stored for the Top500 list; a fresh list fetched 10/10.
- Ranking with `lb ≥ 4.05` + `imdb ≥ 7.5`: *Sinners* 4.10, *One Battle After
  Another* 4.12, *Sentimental Value* 4.15 → 45 points each; below-threshold films
  0–15. Per-film reasons shown.
- Test suites: 57 v1.1 + 35 frontend + 28 interaction + 6 scoring + 5 version
  = **131 checks**.

---

### v1.2.0 — `versions/002-v1.1.0` (current, production packaging)

**Security review (pre-production)**
- **Found & fixed:** no login rate limiting (added username+IP throttle, 8
  failures / 5 min → 15 min lockout) and no CSRF protection (added
  server-signed double-submit token on all form posts).
- **Verified clean:** no SQL injection (f-string sites use hardcoded allowlists),
  no hardcoded secrets, Jinja autoescaping with no `|safe`, all 14 API routes
  authenticated, secrets masked in responses, non-root container.
- **Bug found while testing the CSRF fix:** the token was read from the request
  cookie during render, but the cookie is only set on the *response* — so the
  first page view rendered an empty token and login was impossible (403). Fixed
  by minting the token before render and setting the cookie from the same value.
  Caught only because the live flow was exercised, not unit-tested.
- **Known accepted risk:** the session cookie has no `Secure` flag (LAN HTTP
  deployment). Documented for the operator to address behind a TLS proxy.

**Tests:** 34 security + 53 e2e + 57 v1.1 + 39 frontend + 38 interaction +
6 scoring + 5 version = **232 checks**, with e2e and security also run inside
the built production image.

---

### v1.3.0 — `versions/002-v1.1.0` (current, interim production on .35:7777)

**Added — digital-release filter**
- Settings → Release availability → *Skip films with no digital release yet*
  (default **on**). Held-back films are marked ineligible with a readable reason
  and are **never deleted** — every sync re-checks them, so a film becomes
  eligible automatically once it ships.
- Two data paths:
  - **With a free TMDB key**: `release_dates` is authoritative (`type 4/5/6`
    treated as digital), cached 7 days.
  - **Keyless** (current state): no free source publishes real availability —
    verified during development, Cinemeta carries only a single theatrical
    date — so a conservative heuristic applies (future date, or a current-year
    film with no date, is held back) and the UI states which mode is active
    rather than pretending to certainty. Logs one warning per process.
- New `candidates.released` column (forward-migrated) stores the provider
  release date so availability can be judged without re-fetching metadata.

**Policy adopted — updates never touch operator data**
- The database lives only in the named Docker volume `watchlistarr_data`,
  outside every deploy directory; the image ships an empty `/data`. Verified by
  snapshotting record counts immediately before and after the v1.3.0 deploy:
  **identical** (2 watchlists, 28 candidates, 13 settings, 35 events).
- `scripts/preflight.sh` added so this is proven on every future deploy rather
  than assumed. A drop in any count is a critical failure → roll back.

**Tests:** 33 release-filter + 28 URL + 34 security + 57 v1.1 + 39 frontend +
38 interaction + 6 scoring + 5 version = **240 checks**.

**Automation**
- Daily health check (09:43 UTC) covering logs, restarts, version drift,
  volume/disk growth, SQLite WAL growth and cycle timings; it may fix real bugs
  with a regression test, but is forbidden from touching settings, watchlists,
  the login or tracked movies.
- One-shot reminder on 2026-11-03 to migrate to the production server, including
  the `sqlite3 .backup` procedure for carrying the database across hosts.

---

### v1.3.1 — `versions/002-v1.1.0` (current, interim production on .35:7777)

**CRITICAL ranking fix — reported in production by the operator**

Symptom: "it just requested the top 5 films that were in my watchlist, the latest
that I added." Diagnosis from the live database, not guesswork:

- The watchlist had **3 rules** (genre∈[Sci-Fi, Political, Environmental],
  country∈[India], Letterboxd rating ≥ 4) and `max_per_run=5`.
- Only **3 of 28** candidates scored anything at all (10 pts each, India rule).
- Only **2 distinct score values existed** across the whole list: `0.0` and `10.0`.

Two independent bugs:

1. **No minimum score.** Zero-score films were still *eligible*, so the requester
   filled the remaining slots with films that matched **none** of the rules. With
   `max_per_run=5` and only 3 matches, 2 arbitrary films were requested. New
   per-watchlist **`min_score`** (default `1`): a film must match at least one
   rule. Set `0` to restore the old behaviour deliberately.
2. **Numeric rule values saved as strings.** The UI saves numeric rule operands
   from a text input, so the saved rule was `"value": "4"` (string) rather than
   `4`. `_compare()` did `float(rule.value)`; the string coerced, but the
   Letterboxd-rating rule still never matched because **no ratings had been
   fetched** — so the rule silently contributed nothing. `_rule_number()` now
   coerces strings safely and treats blank/None as *unset* rather than 0, which
   also removes a latent "empty Rating box matches everything at ≥ 0" hazard.

**Also fixed**
- TMDB `release_dates` field name: TMDB returns **`type`**, not `release_type`.
  The digital-release filter read the wrong key, so configuring a TMDB key would
  have marked *every* film as having no digital release (blocking everything).
  Reads `type` with a fallback, and returns None when there is no payload.

**UI**
- **Minimum score to request** field on each watchlist.
- A warning when the rules cannot discriminate: if every film scores the same,
  or no rules exist, say so plainly instead of picking near-arbitrarily.

**Why the rules scored so little (operator guidance, not a bug)**
The watchlist is largely Drama/Comedy with a small Sci-Fi presence and one India
title, so genre∈[Sci-Fi…] matches 1 film and country∈[India] matches 3. With
`min_score=1` correctly, nothing is currently eligible. To get real separation:
press **Fetch Letterboxd ratings** (makes the rating rule work — the largest
single improvement), and/or lower the minimum score to 0.

**Tests:** 18 ranking (new) + 33 release + 28 URL + 34 security + 57 v1.1 +
39 frontend + 38 interaction + 6 scoring + 5 version = **258 checks**.
Data verified unchanged across the deploy (2 watchlists, 28 candidates, 14 settings).

---

### v1.6.4 — `versions/002-v1.1.0` (CURRENT LIVE)

Final state of the 10 Oct session. All four user-reported problems fixed and verified
against live data, not just by test.

**1. Radarr tracked != downloaded** (`test_v16_radarr_paging.py`)
Radarr reported 765 movies but only **276 had files**; 489 were monitored-but-missing.
The old `knows()` treated tracked as "already have it", so **524 of 807 candidates**
were blocked and Request Next had nothing to request. Now `knows()` means *has a file*;
`tracked_but_missing()` is a separate reported state. Verified: 201 of the old 529
matches actually had files, and every tmdbId match was confirmed to be the same film
(title agreed on all 529).

**2. Request Next bypasses the intake valve** (`test_manual_override.py`, v1.6 section)
Manual requests now skip Pace, the Radarr guard, the digital-release gate and the
`max_free_gb` storage ceiling. The `min_free_gb` floor is deliberately kept so a
genuinely full disk can still be protected. Verified live: `{"requested": 1}`, request
825 created in Seerr.

**3. Metadata standardised to TMDB only**
`resolve()` consults TMDB and nothing else — no Cinemeta, no IMDb, no Wikidata, no
country-derived language guess. Match is on **title + release year**, with the year
treated as part of the key so a 1962 *Highway* cannot match a 2014 *Highway*. TMDB's
fuzzy search is validated against the requested title (a search for *Kaul* had been
returning *The Bone Collector*).

**4. Why vs Status separated**
`app/explain.py` is the single renderer. **Why** explains scoring only. **Status** is
just `pending` / `cached` (no digital release yet) — gate reasoning is deliberately
not shown, at the user's request.

**Also in this range**
- Multi-match scoring: a film matching N values of one rule scores N x points, capped
  at 3 via `Rule.max_matches`, so one heavily-tagged film cannot dominate.
- SQL-level pagination + sorters (score, title, year, rating, date added, position).
  Replaced rendering all 801 rows.
- Per-row **Request** button + `POST /api/candidates/{id}/request`.
- Digital release verdict persisted for every film (was showing `?`).

**Bugs found and fixed in this range**
- A limit rule returned `score=0.0` **before** any scoring rule ran, so every film
  read as worthless while merely paused.
- `requestOne` was referenced by `onclick` but never written into the template. Every
  API test passed; only a real click could catch it. `tests/test_request_button.js`
  now asserts every `onclick` handler is defined, and was verified by deleting the
  handler and confirming the test fails.
- `request_single_candidate` called `.get()` on a `sqlite3.Row` (no `.get()`), and
  `_add_event()` was called with the wrong signature. Both caught by tests before deploy.
- Pagination clamped `page` after computing the offset.

**Test totals:** 326 checks across 13 suites.

### v1.3.7 — `versions/002-v1.1.0` (current, interim production on .35:7777)

**Scoring and ratings now automatic** (operator: "these buttons shouldn't exist")

- `sync_watchlist()` now scores immediately after ingesting, and fetches
  Letterboxd ratings for newly discovered films. The **Re-score** and **Fetch
  Letterboxd ratings** buttons were removed from the UI, along with their JS
  handlers. Adding a film to Letterboxd is now sufficient — the scheduler picks
  it up and everything downstream updates.
- Sync responses report `"scored": true` and `lb_ratings`, so automation can
  confirm it happened.
- Ratings backfill endpoint accepts `?limit=` (up to 800) instead of a fixed 60,
  so a pre-existing backlog can be caught up in a couple of calls. Cached slugs
  are skipped, so looping it is cheap and idempotent.

**New: Digital release column**
- Every evaluated film now carries a release verdict
  (`available` / `confidence` / `digital_date` / `note`) and the ranked table
  shows a plain **Yes / No** pill per row, with the reason on hover.

**New: configurable schedule** (Settings → Schedule)
- *Re-check sources every* and *Request cycle every*, from 15 minutes to daily.
- The scheduler re-reads these from the database every 5 minutes, so a change
  takes effect without restarting the container. Env vars remain the fallback.

**Diagnosis note — why scores looked broken**
Scoring was working; the UI was not showing anything useful. Two reasons, both
now addressed:
1. Every film was blocked by the operator's own **Pace** limit (5 requested, 3
   allowed this week), so 800 rows all read `0 / blocked by pace`.
2. With only 3 distinct score values the page gave no sense of ranking. The
   ranked header now states the score range explicitly, e.g. "Scores range 0 to
   10 across 800 films", so the current state is legible at a glance.

**Tests:** 19 pagination + 24 ranking + 33 release + 57 v1.1 + 34 security +
28 URL + 6 scoring + 39 frontend + 38 interaction + 5 version = **283 checks**.

### v1.7.3 — `versions/003-v1.7.0` (current, live on 192.168.1.37:7777)

**Storage ceiling polarity fix & automatic recovery**
- Fixed inverted storage ceiling logic in `app/engine.py` and `app/scoring.py`:
  storage ceiling now correctly pauses automated scheduled intake when free space
  drops *below* the threshold (`free < ceiling`), reserving the safety buffer
  for manual operator requests. Previously it paused when free space was *above*
  the ceiling (`free > ceiling`), completely halting requests on empty/healthy drives (e.g. 605 GB free > 250 GB ceiling).
- Reset 787 candidate records previously stalled in `error` status by the false
  storage ceiling guard back to `pending`.
- Immediate startup request cycle fulfilled overdue frequency slots (4 movies
  requested to Seerr with 201 Created).
- Tests updated: `test_v11.py` and `test_v16_radarr_paging.py` passing with corrected polarity.

**Prior 1.7.x releases:**
- **v1.7.0**: Zero-score gate (`test_v170_min_score_gate.py`) preventing auto-requesting of unranked filler films.
- **v1.7.1**: Language normalization (`test_v171_language_names.py`) mapping bare ISO codes to canonical names.
- **v1.7.2**: Frequency rate pacing (`test_v172_frequency.py`) implementing even-spread rate slots across the quota window.

---

## 8. Next steps

1. **Interim production** at http://192.168.1.35:7777 (one month, then migrate).
2. Feedback on rule-editor ergonomics — the sliders are functional but could be
   more tactile.
3. Investigate the Radarr `/data` zero-free-space reading.
4. v1.1 candidates: Seerr request-state polling (so "requested" becomes
   "downloaded"), bulk approve/reject in the UI, per-watchlist schedule.
5. **November:** migrate to the production server (reminder set for 2026-11-03).
6. Optional: a free TMDB key in Settings upgrades the digital-release filter
   from heuristic to authoritative.

---

## 9. File map

```
versions/001-v1.0.0/
├── app/
│   ├── config.py            settings + env fallbacks
│   ├── db.py                SQLite schema and helpers
│   ├── engine.py            sync → enrich → score → request
│   ├── scoring.py           rule engine + presets
│   ├── timewin.py           day/week/month/quarter/year windows
│   ├── watchlists.py        CRUD + catalogue
│   ├── settings_service.py  integrations + password + sessions
│   ├── main.py              entrypoint
│   ├── sources/             letterboxd.py, imdb_list.py
│   ├── metadata/            providers.py, wikidata.py
│   ├── arr/                 seerr.py, radarr.py
│   └── web/
│       ├── server.py        FastAPI routes
│       ├── templates/       Jinja pages
│       └── static/          style.css, app.js
├── tests/                   test_scoring, test_e2e, mock_arr, test_frontend.js
├── docs/                    SCORING.md, SOURCES.md
├── Dockerfile
├── docker-compose.yml
├── README.md
└── MASTER.md
```