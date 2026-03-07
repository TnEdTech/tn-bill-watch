"""
Flask web dashboard for TN Education Bill Watch.
"""

import json
import os
import threading

from flask import Flask, jsonify, redirect, render_template, request, url_for

from database import Database
from monitor import BillMonitor, STATUS_LABELS

app = Flask(__name__)

DB_PATH = os.environ.get("DB_PATH", "/data/bills.db")

STATUS_COLORS = {
    0: "neutral", 1: "info",    2: "warning", 3: "warning",
    4: "success", 5: "danger",  6: "danger",  7: "purple",
    8: "success", 9: "info",   10: "success", 11: "danger",
    12: "neutral",
}

_scan_lock = threading.Lock()
_scan_running = False
_scan_message = None


def get_db():
    return Database(DB_PATH)


def _parse_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


@app.route("/")
def index():
    db = get_db()
    all_bills = db.get_all_bills()
    events = db.get_recent_events(30)
    last_run = db.get_last_run()

    query = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "")

    base_bills = db.search_bills(query) if query else all_bills

    bills = (
        [b for b in base_bills if str(b["status"]) == status_filter]
        if status_filter
        else base_bills
    )

    status_counts = {}
    for b in all_bills:
        s = b["status"]
        status_counts[s] = status_counts.get(s, 0) + 1

    active_statuses = {1, 2, 3, 9, 10}
    n_active = sum(1 for b in all_bills if b["status"] in active_statuses)
    n_passed = sum(1 for b in all_bills if b["status"] in {4, 8})
    n_failed = sum(1 for b in all_bills if b["status"] in {5, 6, 11})

    return render_template(
        "index.html",
        bills=bills,
        events=events,
        last_run=last_run,
        STATUS_LABELS=STATUS_LABELS,
        STATUS_COLORS=STATUS_COLORS,
        status_filter=status_filter,
        status_counts=status_counts,
        total=len(all_bills),
        n_active=n_active,
        n_passed=n_passed,
        n_failed=n_failed,
        scan_running=_scan_running,
        scan_message=_scan_message,
        query=query,
    )


@app.route("/bill/<int:bill_id>")
def bill_detail(bill_id):
    db = get_db()
    bill = db.get_bill(bill_id)
    if bill is None:
        return "Bill not found", 404

    subjects = [
        s.get("subject_name", s) if isinstance(s, dict) else s
        for s in _parse_list(bill.get("subjects"))
    ]
    sponsors = [
        s.get("name", s) if isinstance(s, dict) else s
        for s in _parse_list(bill.get("sponsors"))
    ]

    all_events = db.get_recent_events(500)
    events = [e for e in all_events if e["bill_id"] == bill_id]

    return render_template(
        "bill.html",
        bill=bill,
        subjects=subjects,
        sponsors=sponsors,
        events=events,
        STATUS_LABELS=STATUS_LABELS,
        STATUS_COLORS=STATUS_COLORS,
    )


@app.route("/scan", methods=["POST"])
def trigger_scan():
    global _scan_running, _scan_message
    with _scan_lock:
        if _scan_running:
            return redirect(url_for("index"))
        _scan_running = True
        _scan_message = None

    def run():
        global _scan_running, _scan_message
        try:
            db = get_db()
            monitor = BillMonitor(os.environ["LEGISCAN_API_KEY"], db)
            new_bills, updated_bills, total = monitor.scan()
            _scan_message = (
                f"Scan complete — {len(new_bills)} new, "
                f"{len(updated_bills)} updated out of {total} education bills found."
            )
        except Exception as exc:
            _scan_message = f"Scan failed: {exc}"
        finally:
            _scan_running = False

    threading.Thread(target=run, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/scan/status")
def scan_status():
    return jsonify({"running": _scan_running, "message": _scan_message})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
