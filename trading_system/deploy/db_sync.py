"""Incremental copy of the recorder database from the cloud VM to the laptop.

Google Cloud's free tier includes only 1 GB/month of download, and the
database grows ~27 MB per trading day, so copying the whole file every time
would exhaust it within weeks. Instead:

    export (on the VM)    python deploy/db_sync.py export <since_id> <since_date> <out.db.gz>
        Writes only snapshots with snapshot_id > since_id (plus their quotes)
        and cycles on or after since_date, into a fresh database with the
        recorder's own schema, gzip-compressed.

    merge (on the laptop) python deploy/db_sync.py merge <increment.db.gz> <target.db>
        Inserts those rows into target (created with the same schema if
        missing). Snapshots/quotes already present are skipped; cycles are
        replaced, since a cycle row gets its finish time after it's first seen.

    since (on the laptop) python deploy/db_sync.py since <target.db>
        Prints "<max snapshot_id> <max cycle trade_date>" for the next export.

Snapshot ids are assigned inside the recorder's write transaction, so a later
commit always gets a higher id and "id > since" never skips a row.
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.option_store import OptionChainStore  # noqa: E402


def since(target: str) -> None:
    if not Path(target).exists():
        print("0 0000-00-00")
        return
    con = sqlite3.connect(target)
    try:
        max_id = con.execute("SELECT COALESCE(MAX(snapshot_id), 0) FROM snapshots").fetchone()[0]
        max_date = con.execute("SELECT COALESCE(MAX(trade_date), '0000-00-00') FROM cycles").fetchone()[0]
    finally:
        con.close()
    print(f"{max_id} {max_date}")


def export(since_id: int, since_date: str, out_gz: str, source: str = "data/options.db") -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plain = str(Path(tmp) / "increment.db")
        OptionChainStore(plain)
        con = sqlite3.connect(plain)
        try:
            con.execute("ATTACH DATABASE ? AS src", (source,))
            con.execute("BEGIN")
            con.execute("INSERT INTO snapshots SELECT * FROM src.snapshots WHERE snapshot_id > ?", (since_id,))
            con.execute(
                "INSERT INTO quotes SELECT * FROM src.quotes WHERE snapshot_id IN (SELECT snapshot_id FROM main.snapshots)"
            )
            con.execute("INSERT INTO cycles SELECT * FROM src.cycles WHERE trade_date >= ?", (since_date,))
            con.commit()
            counts = [con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("snapshots", "quotes", "cycles")]
            con.execute("DETACH DATABASE src")
        finally:
            con.close()
        with open(plain, "rb") as fin, gzip.open(out_gz, "wb", compresslevel=9) as fout:
            shutil.copyfileobj(fin, fout)
    print(f"exported {counts[0]} snapshots, {counts[1]} quotes, {counts[2]} cycles")


def merge(increment_gz: str, target: str) -> None:
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    OptionChainStore(target)
    with tempfile.TemporaryDirectory() as tmp:
        plain = str(Path(tmp) / "increment.db")
        with gzip.open(increment_gz, "rb") as fin, open(plain, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        con = sqlite3.connect(target)
        try:
            con.execute("ATTACH DATABASE ? AS inc", (plain,))
            con.execute("BEGIN")
            before = con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            con.execute("INSERT OR IGNORE INTO snapshots SELECT * FROM inc.snapshots")
            con.execute("INSERT OR IGNORE INTO quotes SELECT * FROM inc.quotes")
            con.execute("INSERT OR REPLACE INTO cycles SELECT * FROM inc.cycles")
            con.commit()
            added = con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] - before
            con.execute("DETACH DATABASE inc")
        finally:
            con.close()
    print(f"merged {added} new snapshots into {target}")


def backup(source: str, dest: str) -> None:
    """Consistent copy of any sqlite3 database via the backup API, so a
    concurrent writer (e.g. a cron job mid-write) can't leave `dest` with a
    half-written file. Generic (not options.db-specific): used to pull
    historical_bars.db too. Positional args only, no inline SQL/Python on
    the command line -- a `python -c "..."` string with embedded quotes
    passed through PowerShell -> ssh -> bash got its quoting mangled and
    silently corrupted into a shell syntax error (confirmed live); this
    subcommand exists specifically so nothing ever needs that again.
    """
    if not Path(source).exists():
        print(f"no such file: {source}")
        return
    s = sqlite3.connect(source)
    d = sqlite3.connect(dest)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    print(f"backed up {source} -> {dest}")


if __name__ == "__main__":
    cmd, *args = sys.argv[1:] or ["help"]
    if cmd == "since" and len(args) == 1:
        since(args[0])
    elif cmd == "export" and len(args) == 3:
        export(int(args[0]), args[1], args[2])
    elif cmd == "merge" and len(args) == 2:
        merge(args[0], args[1])
    elif cmd == "backup" and len(args) == 2:
        backup(args[0], args[1])
    else:
        print(__doc__)
        sys.exit(2)
