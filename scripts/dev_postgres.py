"""Start a project-local PostgreSQL (via pgserver) when Docker is not available.

Usage:  uv run python scripts/dev_postgres.py [data_dir]
Prints an AGENTOS_DATABASE_URL you can export. The server keeps running after this exits;
stop it with:  uv run python scripts/dev_postgres.py --stop
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pgserver

# PostgreSQL's command-line handling breaks on paths containing spaces, and Unix socket paths are
# limited to ~100 characters, so the data directory lives in the system temp dir by default.
DEFAULT_DIR = Path(tempfile.gettempdir()) / "agentos-pgdata"


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    data_dir = Path(args[0]) if args else DEFAULT_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(str(data_dir), cleanup_mode=None)
    if "--stop" in sys.argv:
        server.cleanup()
        print("stopped")
        return
    uri = server.get_uri()  # postgresql://postgres:@/postgres?host=/socket/dir
    print(uri.replace("postgresql://", "postgresql+psycopg://", 1))


if __name__ == "__main__":
    main()
