# Watchlistarr

**Version: 1.6.4**

> ⚠️ **AI-GENERATED CODE — READ THIS FIRST**
> This entire project (application code, tests, documentation) was written by an
> **AI assistant** (Hermes Agent, by Nous Research) under human direction. It is
> provided **as-is, with no warranty of any kind**. It has been exercised against
> live services by its operator, but it has **not** been independently audited,
> security-reviewed, or certified. Review it yourself before trusting it with
> credentials, network exposure, or unattended automation. See
> [`DISCLAIMER.md`](DISCLAIMER.md) for the full statement.

Reads your Letterboxd (and IMDb) watchlists, scores every movie against
point-based priority rules you control, and sends the winners to **Seerr**
(Jellyseerr / Overseerr) as requests.

Built for the ARR ecosystem: it asks ARR for nothing except an API key, keeps its
own state in SQLite, and has no login of its own beyond a single admin password.

- **WebUI:** http://<host>:7777
- **No paid API required.** Every data source is free and keyless.
- **First boot asks you to create a login.** Once, then never again.

---

## What it actually does

```
Letterboxd list ──► metadata (genre/country/language/year/rating)
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
| Films from 1960s China this month | Country=China **+30**, Decade=1960 **+40**, Language=Mandarin **+20**, Quota 4/month (hard) |
| Sci-Fi this week | Genre=Sci-Fi **+40**, Rating≥7 **+15**, Quota 2/week (hard) |
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

- **Seerr URL** — `http://192.168.1.35:5055` (or `http://jellyseerr:5055` on the same Docker network)
- **Seerr API key** — Seerr → Settings → General → API Key → *Show* → copy

Press **Test Seerr connection**. You should see `Connected — Jellyseerr v2.x.x`.

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

- **Quota** — "at most 4 per month". Exhausted means nothing gets requested.
- **Disk / library** — "block while under 100 GB free".

Negative points work as penalties. `Rating ≥ 8 → +20` plus
`Decade is none of 2020s → −15` pushes towards highly rated older films.

The **Ranked pending** table shows the exact reason per movie, so scoring is
never a black box.

---

## Data sources (all free, no login)

| Source | Used for | Status |
|---|---|---|
| Letterboxd | lists & watchlists | primary, reliable |
| Cinemeta | genre, country, year, rating, IMDb/TMDB ids | keyless |
| Wikidata | language (fills Cinemeta's gaps) | keyless, rate-limited |
| IMDb Lists | movie lists | **experimental** — often blocked |

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

**Language sliders match nothing** — language comes from Wikidata, which
rate-limits anonymous callers. Watchlistarr falls back to a
country-of-origin heuristic and marks those rows; wait for the quota to reset for
fully sourced data.

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

See `docs/` for the scoring reference, source notes, and `MASTER.md` for the
full change log.