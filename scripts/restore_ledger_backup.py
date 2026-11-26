"""Decrypt a ledger backup that the server sent to Telegram (niftysell-ledger-*.sqlite3.gz.enc).

    CREDENTIALS_KEY=<the key from deploy/.env> python scripts/restore_ledger_backup.py backup.enc ledger.sqlite3

Writes a plain SQLite file. To put it back on the server: stop the container, copy it over
/app/data/.cache/ledger.sqlite3 in the niftysell_data volume (delete any ledger.sqlite3-wal / -shm beside it), start the
container. Refuses to overwrite an existing output file.
"""

from __future__ import annotations

import gzip
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.credentials import CredentialError, CredentialStore  # noqa: E402


def restore(src: Path, dest: Path, key: str) -> int:
    if dest.exists():
        raise SystemExit(f"{dest} already exists; choose a new file name")
    store = CredentialStore(dest.parent, key, production=True)
    try:
        raw = gzip.decompress(store.decrypt_bytes(src.read_bytes()))
    except (CredentialError, OSError) as exc:
        raise SystemExit(f"Could not restore: {exc}")
    dest.write_bytes(raw)
    with sqlite3.connect(dest) as c:
        if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            dest.unlink()
            raise SystemExit("The decrypted file failed the SQLite integrity check")
        return c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]


def main(argv: list) -> int:
    if len(argv) != 3 or not os.getenv("CREDENTIALS_KEY"):
        print(__doc__)
        return 2
    n = restore(Path(argv[1]), Path(argv[2]), os.environ["CREDENTIALS_KEY"])
    print(f"Restored {argv[2]} ({n} trades). Integrity check ok.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
