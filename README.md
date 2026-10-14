# Watchlistarr

**Version: 1.7.3**

Reads your Letterboxd (and IMDb) watchlists, scores every movie against
point-based priority rules you control, and sends the winners to **Seerr**
(Jellyseerr / Overseerr) as requests.

Built for the ARR ecosystem: it asks ARR for nothing except an API key, keeps its
own state in SQLite, and has no login of its own beyond a single admin password.

- **WebUI:** http://<host>:7777
- **No paid API required.** TMDB (free key) + Letterboxd cover everything.
- **First boot asks you to create a login.** Once, then never again.

---

## What it actually does

```
Letterboxd list ──► metadata (TMDB: genre/country/language/year/rating)
                          │
                          ▼
                   your priority rules
                   (point sliders + limits)
                          │
                          ▼
                 ranked queue  ──► Seerr request
                          │
                          ▼
                  tracked forever (never requested twice)
```

Examples it expresses directly, with no code:

| You want | Build these rules |
|---|---|
| Films from 1960s China this month | Country=China **+30**, Decade=1960 **+40**, Language=Mandarin **+20**, Frequency 4/month |
| Sci-Fi this week | Genre=Sci-Fi **+40**, Rating≥7 **+15**, Frequency 2/week |
| Only when the disk allows | Disk free GB < 100 → block (hard) |

---

## Install

```bash
git clone <this repo> watchlistarr && cd watchlistarr
docker compose up -d --build
```

Then open `http://<host>:7777` and create your login.

The database lives in the `watchlistarr_data` volume. Keep it, or you lose your
settings and request history.

### Connect Seerr

**Settings → Integrations**, paste:

- **Seerr URL** — `http://<seerr-host>:5055` (or `http://jellyseerr:5055` on the same Docker network)
- **Seerr API key** — Seerr → Settings → General → API Key → *Show* → copy

Press **Test Seerr connection**. You should see `Connected — Jellyseerr v2.x.x`.

**A free TMDB API key is required (v1.7.0)** — metadata comes from TMDB only:

- themoviedb.org → account → Settings → API → create a developer key → copy the *API Key (v3 auth)*

**Radarr is optional.** Adding it unlocks disk-space and library-count rules:

- Radarr → Settings → General → API Key

---

## Using it

### Long lists and watchlists

Pagination follows Letterboxd's own "Older" link, so a 29-page member watchlist
(≈800 films) is walked in full. Sync time scales with list size — a first sync of
800 films takes several minutes because each new film needs metadata lookups;
later syncs only process new additions. Use **Max items** on the Source card to
cap a first sync if you want it faster, then raise the cap later.

1. **Add watchlist** (top right). Paste a Letterboxd list URL:
   `https://letterboxd.com/yourname/list/your-list/`
   The username and slug fill themselves in. Leave the slug blank to track a
   member's full watchlist instead.
2. **Sync** — pulls the list and enriches each movie.
3. **Set rules** — pick a preset or build your own sliders.
5. **Request next** — sends the highest-scoring movie to Seerr.
6. The scheduler repeats 2–5 automatically (default every 30 minutes).

### Storage guards (Settings)

Two global pointers in **Settings → Storage guards**, applied to every watchlist:

| Setting | Meaning |
|---|---|
| **Minimum free space (GB)** | Only add films while free space is **at least** this. |
| **Maximum free space (GB)** | Keep adding only while free space is **at or below** this, so the drive is never left mostly empty. |
| **Estimated film size (GB)** | Optional. Stops the last request taking you back over the maximum. |

They can also be set per watchlist as **Min free GB** / **Max free GB** rules.

### Digital release filter

**Settings → Release availability → Skip films with no digital release yet**
(on by default). Films that have not been released digitally are held back and
shown with the reason in the ranked list. They are **never deleted** — every sync
re-checks them, so a film becomes eligible automatically once it ships.

Accuracy: with a free TMDB API key in Settings, real digital-release dates are
used. Without one, a release-date heuristic applies (a future date, or a
current-year film with no date, is held back). No keyless source publishes real
availability, so the app tells you which mode it is in rather than guessing
silently.

### Skipping films you already have

**Settings → Skip films already known → Ignore anything already in Radarr**
(on by default). Watchlistarr matches on TMDB id, IMDb id, or title+year, and
skips anything Radarr already has a record of — **including films whose file you
deleted**, so it will not re-add what you removed on purpose.

### The rules

Each rule is: *when a movie matches this, add these points*. Scores add up;
highest wins.

**Every film is scored and nothing is filtered out.** Films that match none of
your rules score 0 and still queue, below anything that scored. When scores tie,
the order is decided by Letterboxd rating, then IMDb rating, then your own list
order — so the ranking is always meaningful rather than arbitrary. Two rule types are **limits** instead of scorers — they stop
requests outright:

- **Frequency** — "4 per month" means one request every 30 ÷ 4 ≈ 7.5 days: the
  window is split into equal slots and the next slot opens one slot-length
  after the last request. Even spread, not a burst cap.
- **Disk / library** — "block while under 100 GB free".

Negative points work as penalties. `Rating ≥ 8 → +20` plus
`Decade is none of 2020s → −15` pushes towards highly rated older films.

The **Ranked pending** table shows the exact reason per movie, so scoring is
never a black box.

---

## Data sources (all free)

| Source | Used for | Status |
|---|---|---|
| Letterboxd | lists & watchlists | primary, reliable |
| TMDB | **all metadata** (v1.7.0) | free key **required** |
| IMDb Lists | movie lists | **experimental** — often blocked |

Since v1.7.0 every metadata field comes from TMDB only — no Cinemeta, no
Wikidata, no country-derived guesses. A free TMDB key (themoviedb.org) is
required for enrichment; without it films stay unresolved and are retried on
later syncs.

Letterboxd's RSS feeds sit behind a Cloudflare challenge, so Watchlistarr parses
the HTML pages instead. See `docs/SOURCES.md` for detail.

IMDb Lists return HTTP 403 from most server IPs. The adapter is shipped and fails
loudly rather than silently — prefer Letterboxd.

---

## Configuration

Everything is set in the WebUI. Env vars exist as fallbacks:

| Variable | Default | Meaning |
|---|---|---|
| `WATCHLISTARR_PORT` | `7777` | listen port |
| `WATCHLISTARR_DATA` | `/data` | database directory |
| `WATCHLISTARR_SYNC_INTERVAL` | `30` | minutes between syncs (min 5) |
| `WATCHLISTARR_REQUEST_INTERVAL` | `30` | minutes between request cycles |
| `WATCHLISTARR_SCHEDULER` | `true` | disable to run manually only |
| `SEERR_URL` / `SEERR_API_KEY` | — | fallback for the WebUI values |
| `RADARR_URL` / `RADARR_API_KEY` | — | same |

---

## Security

Reviewed and hardened in v1.2.0 before production packaging.

| Area | Status |
|---|---|
| SQL injection | All queries parameterised; the two f-string SQL sites use hardcoded allowlists (column names, migration table names) — no user input reaches a query string |
| Password storage | PBKDF2-SHA256, 260 000 rounds, per-user random salt; plaintext never written |
| Secrets in API | Seerr/Radarr/TMDB keys returned masked (`••••••••`); real values used server-side only |
| Session cookie | `HttpOnly`, `SameSite=Lax`, HMAC-signed, 14-day expiry |
| Brute force | Login throttled: 8 failures per 5 min → 15-minute lockout, keyed on username + client IP |
| CSRF | Signed double-submit token on every form post (login, setup, logout) |
| XSS | Jinja autoescaping on; no `\|safe` anywhere; JS escapes all dynamic strings |
| API auth | All 14 `/api` routes require a valid session |
| Container | Runs as non-root UID 10001; read-only application code |
| Static assets | `Cache-Control: no-cache` + version-stamped URLs |

**Notes for a production deployment**
- Put it behind a TLS-terminating reverse proxy. The session cookie has no
  `Secure` flag because the app is normally reached over plain HTTP on a LAN;
  add `secure=True` (or terminate TLS and set the header) if exposed publicly.
- `proxy_headers=True` is enabled, so `X-Forwarded-For` is honoured for
  throttling — ensure your proxy sets it correctly.
- The throttle is in-process; with multiple replicas it is per-instance.

## Troubleshooting

**"Letterboxd returned HTTP 403"** — Cloudflare rate-limited your IP. Wait, or
lower the sync interval. Your server's shared IP is the usual cause.

**"Seerr is not configured"** — add the URL and key in Settings, press *Test*.

**Sync feels slow** — first sync enriches every new movie over the network. Later
syncs only process new items and are much faster.

**Language sliders match nothing** — check a TMDB key is set in Settings
(v1.7.0: language data comes only from TMDB's original/spoken-language fields).
Films synced before the key was added stay unresolved until a later sync or a
metadata backfill.

---

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
WATCHLISTARR_DATA=/tmp/wl .venv/bin/python -m app     # http://127.0.0.1:7777

.venv/bin/python tests/test_scoring.py   # unit: rule engine
.venv/bin/python tests/test_e2e.py       # integration: live source + mock Seerr
```

`test_e2e.py` hits the real Letterboxd and public metadata APIs, and runs a mock
Seerr/Radarr on 127.0.0.1:5999.

See `docs/` for the scoring reference, source notes, and full change log.

---

## License

This project is licensed under the [MIT License](LICENSE) © 2026 Tanmay Jain. See [`DISCLAIMER.md`](DISCLAIMER.md) for full disclaimer and attribution statements.