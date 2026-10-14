# Data sources

Every source Watchlistarr uses is free and needs no login. This file records what
was actually tested during v1.0.0 development, including the things that did not
work — those matter as much as the successes.

## Letterboxd — primary

**Status: working, keyless, reliable.**

Used for both public lists and member watchlists:

| URL shape | What it gives | Verified |
|---|---|---|
| `https://letterboxd.com/<user>/list/<slug>/` | that list, 100 per page | yes |
| `https://letterboxd.com/<user>/list/<slug>/page/N/` | pagination, no overlap | yes — 854 items over 9 pages |
| `https://letterboxd.com/<user>/watchlist/` | the member's full watchlist | yes |
| `https://letterboxd.com/<user>/lists/` | list discovery for the UI | yes |

Parsing targets `data-item-slug` and `data-item-name`, which carry the
canonical slug and `"Title (Year)"`. The slug is the dedup key, so a movie keeps
its identity across syncs even if the title is re-punctuated.

### What does NOT work

**The RSS feeds are Cloudflare-protected.**

```
/rss/  → 403 Cloudflare challenge
/feed/ → 403
/s/films.xml → 403
```

This holds with a browser User-Agent too. Watchlistarr therefore parses the HTML
pages, which serve fine (`HTTP 200`, ~262 KB for a 100-item page).

### Practical notes

- Requires a normal browser User-Agent; Watchlistarr sends one by default.
- 403 usually means Cloudflare throttled your server's IP, not a broken list.
  Lower `WATCHLISTARR_SYNC_INTERVAL` and retry later.
- `/lists/` renders **relative** hrefs and includes nav links (`/edit/`,
  `/likes/`, `/comments/`) that must be filtered out — a bug fixed in v1.0.0.

---

## TMDB — the single metadata source (v1.7.0)

**Status: required.** Since v1.7.0 (operator decision, 2026-10-14) every metadata
field — title, year, IMDb/TMDB ids, genres, countries, languages, rating,
runtime, release date, poster — comes from exactly one provider: TMDB.

> **History:** v1.6.5 and earlier layered Cinemeta + Wikidata + an IMDb
> suggestion fallback on top of TMDB. In v1.6.5 the keyless path was
> accidentally amputated (`resolve()` returned None without a TMDB key even
> though all the Cinemeta code still shipped), so enrichment silently produced
> blank rows. v1.7.0 made the standardisation honest: the dead providers were
> deleted outright and TMDB became the documented, required source.

Endpoints used:

- `GET /3/search/movie?query=<title>&year=<year>` — fuzzy title search, validated
  against the requested title (the search itself can return unrelated films; a
  "Kaul" search once surfaced *The Bone Collector*, so a plausibility check plus
  exact-year matching runs before anything is stored).
- `GET /3/movie/<id>?append_to_response=release_dates` — the authoritative
  details record. `/search/movie` returns only numeric `genre_ids`; the names
  live here.
- `GET /3/find?external_id=<imdb_id>&external_source=imdb_id` — IMDb-id → TMDB.

Country names are normalised at the ingestion boundary ("United States of
America" → "United States") so saved rules match.

### Key

A free API key (themoviedb.org → Settings → API) is REQUIRED. Without one
`resolve()` returns None, rows are stored unresolved, and later syncs retry
them. The backfill endpoint reports the missing key explicitly instead of
attempting hundreds of doomed lookups.

---

## IMDb Lists — experimental

**Status: implemented, but usually blocked.**

| Endpoint | Result |
|---|---|
| `https://www.imdb.com/list/ls.../` | `HTTP 202` + JS challenge stub, or 403 |
| `https://api.graphql.imdb.com/` | `HTTP 403` |

IMDb's bot protection blocks most datacenter IPs. The adapter is shipped because
it works wherever IMDb serves real HTML (some residential IPs, some cloud exits),
and it **fails loudly** with an explanatory message rather than silently
returning nothing.

The suggestion API does work and is used for id/title resolution:

```
https://v3.sg.media-imdb.com/suggestion/x/<title>.json
```

**Recommendation: use Letterboxd.** IMDb lists are a bonus.

---

## Optional: IMDb suggestion API

**Removed in v1.7.0.** The keyless id-resolution path was deleted together with
Cinemeta; TMDB lookup by IMDb id covers the same need.

---

## Summary

| Source | Login? | Paid? | Role |
|---|---|---|---|
| Letterboxd | no | no | primary movie source |
| TMDB | no | no (free key) | **the** metadata source (required) |
| IMDb Lists | no | no | optional source, often blocked |
| Seerr | n/a | n/a | receives requests (needs your key) |
| Radarr | n/a | n/a | optional capacity rules (needs your key) |