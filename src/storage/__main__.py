"""Командный интерфейс инициализации и диагностики SQLite."""

from __future__ import annotations

import argparse
import asyncio

from .sqlite import SqliteStorage


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Управление SQLite-хранилищем sentinel_key")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("init", "info"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--database", required=True, help="Путь к SQLite или :memory:")
    return parser


async def _run(command: str, database: str) -> None:
    async with SqliteStorage(database) as storage:
        version = await storage.schema_version()
        mode = await storage.journal_mode()
        if command == "init":
            print(f"SQLite initialized: {database} (schema={version}, journal={mode})")
        else:
            print(f"database={database}")
            print(f"schema_version={version}")
            print(f"journal_mode={mode}")


def main() -> None:
    """Разобрать аргументы и выполнить выбранную команду."""
    args = _parser().parse_args()
    asyncio.run(_run(args.command, args.database))


if __name__ == "__main__":
    main()
