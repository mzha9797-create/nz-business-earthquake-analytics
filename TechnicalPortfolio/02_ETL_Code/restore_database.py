"""Restore the supplied SQL snapshot to a separate database."""
from pathlib import Path
import sqlite3
folder=Path(__file__).resolve().parents[1]/"01_Database"
target=folder/"restored_nz_resilience.sqlite"
if target.exists():raise SystemExit("Restore target already exists; choose another output filename.")
with sqlite3.connect(target) as con:
 con.executescript((folder/"restore.sql").read_text(encoding="utf-8"))
 assert con.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
 assert not con.execute("PRAGMA foreign_key_check").fetchall()
 print("Restored earthquake rows:",con.execute("SELECT COUNT(*) FROM fact_earthquake").fetchone()[0])
