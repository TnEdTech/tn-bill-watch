"""
Core monitoring logic: discovers Tennessee education bills via the LegiScan
master list and search API, then persists changes to the database.
"""

import re
import time

from legiscan import LegiScanClient

# ---------------------------------------------------------------------------
# Education subject keywords (matched against LegiScan subject_name fields)
# ---------------------------------------------------------------------------
EDUCATION_SUBJECTS = {
    "education",
    "schools",
    "k-12",
    "higher education",
    "colleges",
    "universities",
    "curriculum",
    "teachers",
    "students",
    "scholarships",
    "special education",
    "charter schools",
    "school finance",
    "school choice",
    "pre-k",
    "preschool",
    "libraries",
    "literacy",
    "stem",
    "vocational",
    "community college",
    "school board",
    "school funding",
}

# ---------------------------------------------------------------------------
# Education keyword patterns (matched against bill title + description text)
# ---------------------------------------------------------------------------
_EDUCATION_PATTERNS = [
    r"\beducation\b",
    r"\bschool\b",
    r"\bschools\b",
    r"\bstudent\b",
    r"\bstudents\b",
    r"\bteacher\b",
    r"\bteachers\b",
    r"\bcurriculum\b",
    r"\bclassroom\b",
    r"\buniversity\b",
    r"\bcollege\b",
    r"\btuition\b",
    r"\bscholarship\b",
    r"\bcharter\b",
    r"\bvoucher\b",
    r"\bliteracy\b",
    r"\bstem\b",
    r"\bcampus\b",
    r"\bschool district\b",
    r"\bsuperintendent\b",
    r"\bprincipal\b",
    r"\btextbook\b",
    r"\blibrary\b",
    r"\bhomeschool\b",
    r"\bpreschool\b",
    r"\bspecial education\b",
    r"\b(i\.?e\.?p)\b",
    r"\bpre-?k\b",
    r"\bacademic\b",
    r"\bgraduation\b",
    r"\benrollment\b",
    r"\bboard of education\b",
    r"\btdoe\b",
]
_COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _EDUCATION_PATTERNS]

# ---------------------------------------------------------------------------
# Title prefixes that identify non-substantive bills to ignore entirely
# ---------------------------------------------------------------------------
_IGNORE_PREFIXES = (
    "a resolution to honor",
    "a resolution to commend",
    "a resolution to congratulate",
    "a resolution to confirm the appointment of",
    "a resolution to recognize"
     
)

# ---------------------------------------------------------------------------
# LegiScan status codes → human-readable labels
# ---------------------------------------------------------------------------
STATUS_LABELS = {
    0: "N/A",
    1: "Introduced",
    2: "Engrossed",
    3: "Enrolled",
    4: "Passed",
    5: "Vetoed",
    6: "Failed",
    7: "Override",
    8: "Chaptered",
    9: "Refer",
    10: "Report Pass",
    11: "Report DNP",
    12: "Draft",
}

# LegiScan search queries used to discover education bills
_SEARCH_QUERIES = [
    "education school",
    "teacher curriculum",
    "student scholarship tuition",
    "charter school voucher",
    "higher education college university",
]


def is_education_related(bill):
    """
    Return True if the bill appears to be education-related, based on:
      1. LegiScan subject tags
      2. Keywords in the title and description

    Returns False for bills matching _IGNORE_PREFIXES regardless of content.
    """
    title = bill.get("title", "")
    if any(title.lower().startswith(p) for p in _IGNORE_PREFIXES):
        return False

    # 1. Check subject tags (present on full bill objects)
    for subj in bill.get("subjects", []):
        name = subj.get("subject_name", "") if isinstance(subj, dict) else str(subj)
        if any(edu in name.lower() for edu in EDUCATION_SUBJECTS):
            return True

    # 2. Keyword scan of title + description
    text = " ".join(
        filter(None, [bill.get("title", ""), bill.get("description", "")])
    )
    return any(pat.search(text) for pat in _COMPILED_PATTERNS)


class BillMonitor:
    def __init__(self, api_key, db, state="TN"):
        self.client = LegiScanClient(api_key)
        self.db = db
        self.state = state

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def scan(self, verbose=False):
        """
        Scan LegiScan for education-related bills.

        Strategy:
          1. Pull the full master list for the current TN session.
          2. Filter by title/description keywords.
          3. Also run targeted search queries for broader discovery.
          4. For each candidate, fetch full bill detail and upsert into DB.
          5. For bills already tracked, only re-fetch if change_hash differs.

        Returns: (new_bills, updated_bills, total_education_bills)
          new_bills     – list of full bill dicts for newly discovered bills
          updated_bills – list of (bill, event_type, old_val, new_val) tuples
          total         – total number of education bills detected this run
        """
        session = self.client.get_current_session(self.state)
        session_id = session["session_id"]

        if verbose:
            print(
                f"Session: {session.get('session_name', session_id)} "
                f"(id={session_id})"
            )

        # --- Step 1: master list candidate IDs -------------------------
        master_list = self.client.get_master_list(session_id)
        if verbose:
            print(f"Total bills in session: {len(master_list)}")

        master_candidates = {
            b["bill_id"]: b
            for b in master_list
            if is_education_related(b)
        }

        # --- Step 2: search API candidate IDs --------------------------
        search_ids = self._search_candidates(verbose=verbose)

        # --- Step 3: merge candidate sets -------------------------------
        all_candidate_ids = set(master_candidates) | search_ids

        if verbose:
            print(
                f"Education bill candidates: {len(all_candidate_ids)} "
                f"({len(master_candidates)} from master list, "
                f"{len(search_ids)} from search)"
            )

        # --- Step 4: fetch details and upsert ---------------------------
        known = self.db.get_known_bill_ids()  # {bill_id: change_hash}
        new_bills = []
        updated_bills = []

        candidates = sorted(all_candidate_ids)
        for idx, bill_id in enumerate(candidates, 1):
            # Skip if already tracked and hash hasn't changed
            master_hash = master_candidates.get(bill_id, {}).get("change_hash", "")
            if bill_id in known and known[bill_id] == master_hash and master_hash:
                continue

            if verbose and idx % 20 == 0:
                print(f"  Fetching details {idx}/{len(candidates)}...")

            try:
                bill = self.client.get_bill(bill_id)
                if not is_education_related(bill):
                    continue
                events = self.db.upsert_bill(bill)
                for event_type, old_val, new_val in events:
                    if event_type == "new":
                        new_bills.append(bill)
                    elif event_type in ("status_change", "updated"):
                        updated_bills.append((bill, event_type, old_val, new_val))
            except Exception as exc:
                if verbose:
                    print(f"  Warning: could not fetch bill {bill_id}: {exc}")

            # Be polite to the API
            time.sleep(0.25)

        self.db.log_run(len(all_candidate_ids), len(new_bills), len(updated_bills))
        return new_bills, updated_bills, len(all_candidate_ids)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _search_candidates(self, verbose=False):
        """Run several keyword searches and return a set of bill IDs."""
        found_ids = set()
        for query in _SEARCH_QUERIES:
            try:
                result = self.client.search(query, state=self.state)
                for item in result.get("results", {}).values():
                    if isinstance(item, dict) and "bill_id" in item:
                        found_ids.add(item["bill_id"])
            except Exception as exc:
                if verbose:
                    print(f"  Search warning ({query!r}): {exc}")
            time.sleep(0.3)
        return found_ids
