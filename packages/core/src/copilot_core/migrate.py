"""Apply the plain SQL migrations in ``migrations/`` in filename order.

Each file runs in its own transaction and is recorded in ``public.schema_migrations``
with a checksum, so an applied migration that is edited later is reported instead of
silently skipped. An advisory lock keeps two runners from applying at the same time.

Usage (from the repo root)::

    uv run copilot-migrate                 # apply pending migrations
    uv run copilot-migrate --status        # list applied and pending

The connection comes from ``COPILOT_DATABASE_URL`` (or ``--database-url``).
"""

import argparse
import asyncio
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg

DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/copilot"
LOCK_ID = 0x636F70696C6F74  # "copilot"
FILENAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def load_migrations(directory: Path) -> list[Migration]:
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = FILENAME.match(path.name)
        if not match:
            raise SystemExit(f"bad migration filename {path.name}: expected NNNN_name.sql")
        # Normalize line endings so the checksum is the same on Windows and Linux.
        sql = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        migrations.append(Migration(match.group(1), path.stem, sql))
    versions = [m.version for m in migrations]
    if len(set(versions)) != len(versions):
        raise SystemExit(f"duplicate migration version in {directory}")
    return migrations


async def apply(database_url: str, directory: Path, *, status_only: bool = False) -> list[str]:
    """Apply pending migrations; return the names of those applied (or pending)."""
    migrations = load_migrations(directory)
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute("select pg_advisory_lock($1)", LOCK_ID)
        await conn.execute(
            """
            create table if not exists public.schema_migrations (
              version    text primary key,
              name       text not null,
              checksum   text not null,
              applied_at timestamptz not null default now()
            )
            """
        )
        applied = {
            r["version"]: r
            for r in await conn.fetch(
                "select version, name, checksum from public.schema_migrations"
            )
        }
        done: list[str] = []
        for m in migrations:
            row = applied.get(m.version)
            if row is not None:
                if row["checksum"] != m.checksum:
                    raise SystemExit(
                        f"migration {m.name} was changed after it was applied; "
                        "add a new migration instead"
                    )
                continue
            if status_only:
                done.append(m.name)
                continue
            async with conn.transaction():
                # Record first: a migration may `set local role`, which lasts until commit.
                # If the migration fails, the transaction rolls this row back too.
                await conn.execute(
                    "insert into public.schema_migrations (version, name, checksum)"
                    " values ($1, $2, $3)",
                    m.version,
                    m.name,
                    m.checksum,
                )
                await conn.execute(m.sql)
            done.append(m.name)
        return done
    finally:
        await conn.close()


def run() -> None:
    parser = argparse.ArgumentParser(description="Apply SQL migrations in order.")
    parser.add_argument(
        "--database-url", default=os.environ.get("COPILOT_DATABASE_URL", DEFAULT_DATABASE_URL)
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=Path(os.environ.get("COPILOT_MIGRATIONS_DIR", "migrations")),
        help="migrations directory (default: ./migrations)",
    )
    parser.add_argument("--status", action="store_true", help="list pending migrations only")
    args = parser.parse_args()
    if not args.dir.is_dir():
        sys.exit(f"migrations directory not found: {args.dir} (run from the repo root)")
    names = asyncio.run(apply(args.database_url, args.dir, status_only=args.status))
    verb = "pending" if args.status else "applied"
    print(f"{len(names)} migration(s) {verb}" + "".join(f"\n  {n}" for n in names))
