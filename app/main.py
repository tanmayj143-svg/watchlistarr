"""Watchlistarr entrypoint: `python -m app` or `python app/main.py`."""
from __future__ import annotations

import logging
import os
import sys

# Allow both `python -m app` (package) and `python app/main.py` (script).
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=os.getenv("WATCHLISTARR_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)-24s %(message)s",
    )
    log = logging.getLogger("watchlistarr")
    log.info("Watchlistarr starting — http://%s:%s", settings.host, settings.port)
    # Only pass kwargs this uvicorn version accepts, so startup never dies on
    # a signature difference (uvicorn.run rejects unknown kwargs).
    uvicorn.run(
        "app.web.server:app",
        host=settings.host,
        port=settings.port,
        log_level=os.getenv("WATCHLISTARR_LOG_LEVEL", "info").lower(),
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()