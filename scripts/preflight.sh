#!/usr/bin/env bash
# Watchlistarr deploy preflight / postflight.
#
# Purpose: guarantee an update NEVER touches the operator's settings,
# watchlists, tracked movies or request history.
#
# The database lives in the named Docker volume `watchlistarr_data`, which is
# outside any deploy directory and is never written by the image build. This
# script proves that empirically instead of trusting it.
set -uo pipefail

CONTAINER="${1:-watchlistarr}"
VOLUME="${WATCHLISTARR_VOLUME:-watchlistarr_data}"
SNAP=/tmp/wlarr_snapshot_before.txt

snapshot() {
  docker exec "$CONTAINER" python - <<'PY' 2>/dev/null
import sqlite3
c = sqlite3.connect("/data/watchlistarr.db")
out = []
for t in ("watchlists", "candidates", "settings", "candidate_events", "runs"):
    try:
        out.append("%s=%s" % (t, c.execute("select count(*) from " + t).fetchone()[0]))
    except Exception:
        out.append("%s=?" % t)
print(" ".join(out))
PY
}

if [ "${1:-}" = "--snapshot" ]; then
  snapshot
  exit 0
fi

BEFORE=$(docker exec "$CONTAINER" python -c "
import sqlite3
c=sqlite3.connect('/data/watchlistarr.db')
print(' '.join('%s=%s'%(t,c.execute('select count(*) from '+t).fetchone()[0]) for t in ('watchlists','candidates','settings')))
" 2>/dev/null || echo "")

if [ -z "$BEFORE" ]; then
  echo "PREFLIGHT FAILED: cannot read the database in $CONTAINER"
  exit 1
fi

echo "preflight  $BEFORE"
echo "$BEFORE" > "$SNAP"
echo "volume     $VOLUME (must be a named volume, not a bind mount)"
docker inspect "$CONTAINER" --format '{{range .Mounts}}{{if eq .Type "volume"}}  volume: {{.Name}} -> {{.Destination}}{{end}}{{end}}'
