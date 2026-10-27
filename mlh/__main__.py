"""Run the backend: python -m mlh

Single process, single worker, no auto-reload: the process owns the chDB data
directory (flock) and every RMV, so it must not be duplicated.
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from .api import create_app
from .chdb_owner import AlreadyRunning
from .settings import load_settings


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    try:
        uvicorn.run(create_app(settings), host=settings.bind_host, port=settings.port,
                    workers=1, reload=False, log_level="info", server_header=False)
    except AlreadyRunning as exc:
        logging.getLogger("mlh").error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
