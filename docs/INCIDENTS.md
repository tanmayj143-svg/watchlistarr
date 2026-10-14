# Watchlistarr Incidents & Remediations Log

## Incident 2026-10-14 — Inverted Storage Ceiling Guard Halting Requests

- **Date / Time:** 2026-10-14 11:15 UTC (16:45 IST)
- **Severity:** High (Frequency slot overdue >10h, all automated candidate requesting blocked)
- **Component:** `app/engine.py` (`_apply_storage_guards`), `app/scoring.py` (`_max_free_match`), SQLite database (`candidates` table)

### Root Cause
The storage ceiling guard logic was inverted: `_apply_storage_guards` checked `if free > max_free: return "storage ceiling..."`. When the Radarr library had 605.5 GB free and `max_free_gb` was set to 250 GB in settings, the engine treated healthy free space as exceeding the ceiling and halted automated top-up requests. 787 candidate rows were subsequently marked with `status='error'` and `last_reason='storage ceiling: 605.5 GB free is above the 250 GB ceiling, topping up paused'`.

### Remediation Applied
1. **Code Fix (v1.7.3):**
   - Corrected guard polarity in `app/engine.py`: `if max_free is not None and free < max_free: ...`
   - Corrected scorer guard polarity in `app/scoring.py`: `if free < th: ...` and `if est > 0 and (free - est) < th: ...`
   - Updated test suites `test_v11.py` and `test_v16_radarr_paging.py`.
   - Bumped version to `1.7.3` across `app/config.py`, `docker-compose.yml`, `Dockerfile`, and `README.md`.
2. **Database State Recovery:**
   - Reset 787 candidates with `last_reason LIKE 'storage ceiling%'` from `status='error'` back to `status='pending'`.
3. **Container Deployment:**
   - Built `watchlistarr:1.7.3` image.
   - Deployed container `watchlistarr` via standard non-destructive container swap.
   - Verified `/healthz` returned `{"ok":true,"app":"watchlistarr","version":"1.7.3"}`.
   - Removed intermediate `watchlistarr-old` container and `watchlistarr:1.7.2` image.
4. **Verification & Discharge:**
   - Startup request cycle executed immediately.
   - 4 top-ranked eligible candidates requested to Seerr (201 Created).
   - Frequency slots fulfilled; candidates table verified at 790 pending, 18 requested.
