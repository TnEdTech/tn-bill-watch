"""
SQLite storage for tracked bills and change events.
"""

import json
import sqlite3
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, db_path="bills.db"):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS bills (
                bill_id       INTEGER PRIMARY KEY,
                session_id    INTEGER,
                bill_number   TEXT,
                title         TEXT,
                description   TEXT,
                status        INTEGER,
                status_date   TEXT,
                change_hash   TEXT,
                url           TEXT,
                subjects      TEXT,
                sponsors      TEXT,
                first_seen    TEXT,
                last_updated  TEXT
            );

            CREATE TABLE IF NOT EXISTS bill_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                bill_id     INTEGER,
                event_type  TEXT,
                old_value   TEXT,
                new_value   TEXT,
                recorded_at TEXT
            );

            CREATE TABLE IF NOT EXISTS runs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at        TEXT,
                bills_found   INTEGER,
                new_bills     INTEGER,
                updated_bills INTEGER
            );
        """)
        self.conn.commit()

    # ------------------------------------------------------------------
    # Bills
    # ------------------------------------------------------------------

    def upsert_bill(self, bill):
        """
        Insert or update a bill record.

        Returns a list of event tuples: (event_type, old_value, new_value).
        event_type is one of: 'new', 'status_change', 'updated'.
        """
        existing = self.get_bill(bill["bill_id"])
        now = _now()
        events = []

        subjects_json = json.dumps(bill.get("subjects", []))
        sponsors_json = json.dumps(bill.get("sponsors", []))

        if existing is None:
            self.conn.execute(
                """
                INSERT INTO bills
                    (bill_id, session_id, bill_number, title, description,
                     status, status_date, change_hash, url,
                     subjects, sponsors, first_seen, last_updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    bill["bill_id"],
                    bill.get("session_id"),
                    bill.get("bill_number", ""),
                    bill.get("title", ""),
                    bill.get("description", ""),
                    bill.get("status", 0),
                    bill.get("status_date", ""),
                    bill.get("change_hash", ""),
                    bill.get("url", ""),
                    subjects_json,
                    sponsors_json,
                    now,
                    now,
                ),
            )
            events.append(("new", None, None))

        elif existing["change_hash"] != bill.get("change_hash", ""):
            old_status = existing["status"]
            new_status = bill.get("status", old_status)
            if old_status != new_status:
                events.append(("status_change", str(old_status), str(new_status)))
            else:
                events.append(("updated", existing["change_hash"], bill.get("change_hash", "")))

            self.conn.execute(
                """
                UPDATE bills
                SET status=?, status_date=?, change_hash=?,
                    title=?, description=?, subjects=?, sponsors=?,
                    last_updated=?
                WHERE bill_id=?
                """,
                (
                    new_status,
                    bill.get("status_date", existing["status_date"]),
                    bill.get("change_hash", ""),
                    bill.get("title", existing["title"]),
                    bill.get("description", existing["description"]),
                    subjects_json,
                    sponsors_json,
                    now,
                    bill["bill_id"],
                ),
            )

        for event_type, old_val, new_val in events:
            self.conn.execute(
                """
                INSERT INTO bill_events (bill_id, event_type, old_value, new_value, recorded_at)
                VALUES (?,?,?,?,?)
                """,
                (bill["bill_id"], event_type, old_val, new_val, now),
            )

        self.conn.commit()
        return events

    def get_bill(self, bill_id):
        row = self.conn.execute(
            "SELECT * FROM bills WHERE bill_id=?", (bill_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_bills(self):
        rows = self.conn.execute(
            "SELECT * FROM bills ORDER BY last_updated DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def search_bills(self, query):
        pattern = f"%{query}%"
        rows = self.conn.execute(
            """
            SELECT * FROM bills
            WHERE bill_number LIKE ?
               OR title LIKE ?
               OR description LIKE ?
               OR sponsors LIKE ?
               OR subjects LIKE ?
            ORDER BY last_updated DESC
            """,
            (pattern, pattern, pattern, pattern, pattern),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_known_bill_ids(self):
        rows = self.conn.execute("SELECT bill_id, change_hash FROM bills").fetchall()
        return {r["bill_id"]: r["change_hash"] for r in rows}

    # ------------------------------------------------------------------
    # Events & runs
    # ------------------------------------------------------------------

    def get_recent_events(self, limit=50):
        rows = self.conn.execute(
            """
            SELECT be.*, b.bill_number, b.title, b.url
            FROM bill_events be
            JOIN bills b ON be.bill_id = b.bill_id
            ORDER BY be.recorded_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_last_run(self):
        row = self.conn.execute(
            "SELECT * FROM runs ORDER BY run_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def log_run(self, bills_found, new_bills, updated_bills):
        self.conn.execute(
            "INSERT INTO runs (run_at, bills_found, new_bills, updated_bills) VALUES (?,?,?,?)",
            (_now(), bills_found, new_bills, updated_bills),
        )
        self.conn.commit()
