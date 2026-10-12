"""Guard against the version string drifting from the Docker tag / README.

`/healthz` reported 1.0.0 after a 1.1.0 deploy because APP_VERSION, the compose
image tag and the docs each carried their own copy. This test asserts they agree.

Run: python tests/test_version_consistency.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PASS = FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}{(' — ' + extra) if extra else ''}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


cfg = (ROOT / "app" / "config.py").read_text()
m = re.search(r'APP_VERSION\s*=\s*"([\d.]+)"', cfg)
app_version = m.group(1) if m else None
check("APP_VERSION found in app/config.py", bool(app_version), str(app_version))

compose = (ROOT / "docker-compose.yml").read_text()
tag = re.search(r"image:\s*watchlistarr:([\d.]+)", compose)
compose_version = tag.group(1) if tag else None
check("compose image tag matches APP_VERSION",
      compose_version == app_version, f"compose={compose_version} app={app_version}")

dockerfile = (ROOT / "Dockerfile").read_text()
label = re.search(r'org\.opencontainers\.image\.version="([\d.]+)"', dockerfile)
docker_version = label.group(1) if label else None
check("Dockerfile label matches APP_VERSION",
      docker_version == app_version, f"dockerfile={docker_version} app={app_version}")

readme = (ROOT / "README.md").read_text()
check("README mentions the version", app_version in readme, app_version)

# The health endpoint must report the same string.
if app_version:
    import os
    import tempfile

    os.environ.setdefault("WATCHLISTARR_DATA", tempfile.mkdtemp(prefix="wlarr-ver-"))
    sys.path.insert(0, str(ROOT))
    from app.config import APP_VERSION  # noqa: E402

    check("app.config.APP_VERSION == parsed literal", APP_VERSION == app_version, APP_VERSION)

print(f"\n{'=' * 58}\n  {PASS} passed, {FAIL} failed\n{'=' * 58}")
sys.exit(1 if FAIL else 0)