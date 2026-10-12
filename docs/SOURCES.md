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

## Cinemeta — metadata, keyless

`https://v3-cinemeta.strem.io` — the public Stremio metadata service. No key, no
account, no rate-limit complaints observed.

- `GET /meta/movie/tt<imdb_id>.json` → genre, country, director, runtime,
  rating, IMDb id, TMDB id, description, poster
- `GET /catalog/movie/top/search=<title>.json` → title search

**Verified** on "Farewell My Concubine": genres `['Drama','Music','Romance']`,
countries `['China','Hong Kong','South Korea']`, IMDb `tt0106332`, TMDB `10997`.

### Known gap

`language` is usually **absent**. Watchlistarr fills it from Wikidata, then falls
back to a country heuristic. See below.

Posters are requested at `medium` size by rewriting `/small/` in the returned URL.

---

## Wikidata — language backfill, keyless

`https://www.wikidata.org/w/api.php` (`wbsearchentities` + `wbgetentities`).

Needed because Cinemeta leaves language blank, and language sliders would
otherwise match nothing. Properties read:

| Property | Meaning |
|---|---|
| `P345` | IMDb id (used to confirm the right entity) |
| `P364` | original language |
| `P495` | country of origin |
| `P136` | genre |

**Verified**: "Farewell My Concubine" → `Q696413`, `P364` → `Q24841726`
("Putonghua" → normalised to **Mandarin**), `P495` → `Q148` ("People's Republic of
China" → normalised to **China**).

Those normalisations matter: without them a country slider set to "China" would
never match Wikidata's longer label.

### Rate limiting — the main operational caveat

Wikidata aggressively throttles anonymous callers. During development this IP
received sustained `HTTP 429 Too Many Requests`.

Mitigations in place:

- every call passes through a **slot-based throttle** (3 slots, 0.6 s apart)
- failures **retry once**, then give up fast rather than stalling a sync
- results are cached, so repeat lookups cost nothing

### When Wikidata is unavailable

`resolve()` falls back to a **country → dominant language** heuristic
(`COUNTRY_LANGUAGE`), and flags the row as guessed. Verified by simulating a
total outage: all five test films still resolved a language, so language sliders
keep working.

The heuristic is conservative — only countries with one dominant language — and
only applies when both Cinemeta *and* Wikidata are silent.

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

## Optional: TMDB

Only used if you paste a free API key into Settings. TMDB is more precise on
country-of-origin and language, so it improves match quality — but Watchlistarr
is fully functional without it.

---

## Summary

| Source | Login? | Paid? | Role |
|---|---|---|---|
| Letterboxd | no | no | primary movie source |
| Cinemeta | no | no | genre, country, year, rating, ids |
| Wikidata | no | no | language, country refinement |
| IMDb suggestion | no | no | id/title resolution |
| IMDb Lists | no | no | optional source, often blocked |
| TMDB | no | no | optional accuracy boost |
| Seerr | n/a | n/a | receives requests (needs your key) |
| Radarr | n/a | n/a | optional capacity rules (needs your key) |