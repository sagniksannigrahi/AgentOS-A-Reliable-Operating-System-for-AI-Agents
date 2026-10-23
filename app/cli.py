"""Operational CLI.

python -m app.cli migrate                     # alembic upgrade head + checkpointer tables
python -m app.cli create-key --name me --scopes tasks:read,tasks:write,approvals:read,approvals:decide
python -m app.cli api | worker
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.config.settings import PROJECT_ROOT, get_settings


def migrate(database_url: str | None = None) -> None:
    from alembic import command
    from alembic.config import Config

    url = database_url or get_settings().database_url
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.attributes["database_url"] = url
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    asyncio.run(_setup_checkpointer(url))


async def _setup_checkpointer(url: str) -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    conninfo = url.replace("postgresql+psycopg://", "postgresql://", 1)
    async with AsyncPostgresSaver.from_conn_string(conninfo) as saver:
        await saver.setup()


async def _create_key(name: str, scopes: list[str]) -> str:
    from app.persistence.db import Database
    from app.persistence.repository import create_api_key

    db = Database(get_settings())
    try:
        async with db.session() as s:
            _, plaintext = await create_api_key(s, name, scopes)
    finally:
        await db.dispose()
    return plaintext


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agentos")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    ck = sub.add_parser("create-key")
    ck.add_argument("--name", required=True)
    ck.add_argument("--scopes", default="tasks:read,tasks:write,approvals:read,approvals:decide")
    sub.add_parser("api")
    sub.add_parser("worker")
    args = parser.parse_args(argv)
    if args.cmd == "migrate":
        migrate()
        print("migrations applied")
    elif args.cmd == "create-key":
        key = asyncio.run(_create_key(args.name, [s.strip() for s in args.scopes.split(",") if s.strip()]))
        print("API key (shown once, store it securely):", file=sys.stderr)
        print(key)
    elif args.cmd == "api":
        from app.main import run

        run()
    elif args.cmd == "worker":
        from app.worker.main import run as run_worker

        run_worker()


if __name__ == "__main__":
    main()
