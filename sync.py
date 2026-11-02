"""
Stripe -> Google Sheets sync (demo / sample build).

What it does, in plain English:
  1. Asks Stripe for every payment (a "charge") from the last LOOKBACK_DAYS days.
  2. Turns each one into a clean spreadsheet row.
  3. Merges those rows with what's already in the sheet. New payments are added,
     and payments that changed (for example, got refunded) are updated in place.
  4. Rebuilds a "Daily totals" tab: money in, refunds and net, per day.

If the Google secrets aren't set, it writes the same two tables to CSV files in
data/ instead, so you can run it on your own computer with only a Stripe key.

Settings come from environment variables (GitHub repository secrets):
  STRIPE_API_KEY               required, must be a TEST key (starts with sk_test_ or rk_test_)
  GOOGLE_SERVICE_ACCOUNT_JSON  optional, the whole service-account key file as text
  GOOGLE_SHEET_ID              optional, the long ID from the sheet's web address
"""

import csv
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

STRIPE_API = "https://api.stripe.com/v1"
LOOKBACK_DAYS = 30        # re-check this many days back so late refunds get picked up
PAGE_SIZE = 100           # Stripe's maximum per request
MAX_RETRIES = 4
TIMEOUT = 20

# Column order for the "Payments" tab. Card numbers are deliberately NOT copied:
# the sheet only needs the card brand, so no sensitive card data leaves Stripe.
PAYMENT_FIELDS = [
    "charge_id", "created_utc", "amount", "currency", "status",
    "refunded_amount", "net_amount", "customer_email", "description", "card_brand",
]
TOTAL_FIELDS = ["date", "payments", "gross", "refunds", "net"]

DATA_DIR = Path("data")
LOG_DIR = Path("logs")
log = logging.getLogger("sync")


def setup_logging():
    """Log to the screen and to logs/sync.log."""
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_DIR / "sync.log")],
    )


def get_test_key():
    """Read the Stripe key and refuse to run with a live (real-money) key."""
    key = os.environ.get("STRIPE_API_KEY", "").strip()
    if not key:
        raise SystemExit("STRIPE_API_KEY is not set.")
    if not key.startswith(("sk_test_", "rk_test_")):
        raise SystemExit("This demo only runs with a Stripe TEST key (sk_test_... or rk_test_...).")
    return key


def stripe_get(session, path, params):
    """
    GET one page from the Stripe API, retrying when Stripe says "slow down" (429)
    or has a temporary server error (5xx). Waits 1s, 2s, 4s between tries.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(f"{STRIPE_API}/{path}", params=params, timeout=TIMEOUT)
        except (requests.ConnectionError, requests.Timeout) as err:
            problem = str(err)                # network hiccup: worth retrying
        else:
            if resp.status_code != 429 and resp.status_code < 500:
                resp.raise_for_status()       # other errors (bad key, etc.) stop right away
                return resp.json()
            problem = f"Stripe returned {resp.status_code}"
        if attempt == MAX_RETRIES:
            raise RuntimeError(f"Gave up after {MAX_RETRIES} tries: {problem}")
        wait = 2 ** (attempt - 1)
        log.warning("Attempt %d failed (%s), retrying in %ss", attempt, problem, wait)
        time.sleep(wait)


def fetch_charges(session, since):
    """
    Get every charge created on or after `since`, following Stripe's pagination.
    Stripe returns 100 at a time plus has_more; we pass the last ID as
    starting_after to get the next page.
    """
    charges, starting_after = [], None
    while True:
        params = {"limit": PAGE_SIZE, "created[gte]": int(since.timestamp())}
        if starting_after:
            params["starting_after"] = starting_after
        page = stripe_get(session, "charges", params)
        charges.extend(page["data"])
        log.info("Fetched %d charges so far", len(charges))
        if not page.get("has_more") or not page["data"]:
            return charges
        starting_after = page["data"][-1]["id"]


def charge_to_row(charge):
    """Turn one Stripe charge (a big nested dict) into one flat spreadsheet row."""
    amount = charge["amount"] / 100            # Stripe counts in cents
    refunded = charge.get("amount_refunded", 0) / 100
    billing = charge.get("billing_details") or {}
    card = (charge.get("payment_method_details") or {}).get("card") or {}
    status = "refunded" if charge.get("refunded") else (
        "partially refunded" if refunded else charge.get("status", ""))
    return {
        "charge_id": charge["id"],
        # The " UTC" ending keeps Google Sheets from re-formatting it as a local date,
        # so it reads back exactly as written.
        "created_utc": datetime.fromtimestamp(charge["created"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "amount": f"{amount:.2f}",
        "currency": charge.get("currency", "").upper(),
        "status": status,
        "refunded_amount": f"{refunded:.2f}",
        "net_amount": f"{amount - refunded:.2f}",
        "customer_email": billing.get("email") or charge.get("receipt_email") or "",
        "description": charge.get("description") or "",
        "card_brand": card.get("brand", ""),
    }


def _same(a, b):
    """
    Compare two cell values. Google Sheets hands numbers back as numbers
    (51 instead of "51.00"), so compare numerically when both look like numbers.
    """
    try:
        return abs(float(a) - float(b)) < 0.005
    except (TypeError, ValueError):
        return str(a) == str(b)


def merge_rows(existing, fresh):
    """
    Combine rows already in the sheet with rows just pulled from Stripe.
    - Same charge_id: the fresh version wins (picks up refunds and status changes).
    - Only in the sheet: kept (older than the look-back window).
    - Only in Stripe: added.
    Returns (rows newest first, count added, count updated).
    """
    by_id = {r["charge_id"]: r for r in existing}
    added = updated = 0
    for row in fresh:
        old = by_id.get(row["charge_id"])
        if old is None:
            added += 1
        elif any(_same(old.get(f, ""), row[f]) is False for f in PAYMENT_FIELDS):
            updated += 1
        by_id[row["charge_id"]] = row
    rows = sorted(by_id.values(), key=lambda r: r["created_utc"], reverse=True)
    return rows, added, updated


def daily_totals(rows):
    """Roll payments up into one line per day: count, gross, refunds, net."""
    days = defaultdict(lambda: {"payments": 0, "gross": 0.0, "refunds": 0.0})
    for r in rows:
        if r["status"] == "failed":
            continue                          # failed payments brought in no money
        day = days[r["created_utc"][:10]]
        day["payments"] += 1
        day["gross"] += float(r["amount"])
        day["refunds"] += float(r["refunded_amount"])
    return [
        {"date": d, "payments": v["payments"], "gross": f"{v['gross']:.2f}",
         "refunds": f"{v['refunds']:.2f}", "net": f"{v['gross'] - v['refunds']:.2f}"}
        for d, v in sorted(days.items(), reverse=True)
    ]


def as_table(rows, fields):
    """Header row + data rows, the shape both CSV and Google Sheets want."""
    return [fields] + [[r[f] for f in fields] for r in rows]


# ---------- where the rows live: Google Sheet, or CSV files as a fallback ----------

class SheetStore:
    """Reads and writes the 'Payments' and 'Daily totals' tabs of one Google Sheet."""

    def __init__(self, creds_json, sheet_id):
        import gspread                        # only needed when Sheets is turned on
        client = gspread.service_account_from_dict(json.loads(creds_json))
        self.book = client.open_by_key(sheet_id)

    def _tab(self, title, cols):
        try:
            return self.book.worksheet(title)
        except Exception:
            return self.book.add_worksheet(title=title, rows=1000, cols=cols)

    def read_payments(self):
        return self._tab("Payments", len(PAYMENT_FIELDS)).get_all_records()

    def write(self, title, table):
        ws = self._tab(title, len(table[0]))
        ws.clear()
        # USER_ENTERED makes "51.00" a real number, so the sheet can sum it
        ws.update(values=table, range_name="A1", value_input_option="USER_ENTERED")
        ws.freeze(rows=1)


class CsvStore:
    """Same interface as SheetStore, but saves CSV files in data/."""

    def read_payments(self):
        path = DATA_DIR / "payments.csv"
        if not path.exists():
            return []
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def write(self, title, table):
        DATA_DIR.mkdir(exist_ok=True)
        name = title.lower().replace(" ", "_") + ".csv"
        with (DATA_DIR / name).open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(table)


def pick_store():
    creds, sheet_id = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"), os.environ.get("GOOGLE_SHEET_ID")
    if creds and sheet_id:
        log.info("Writing to Google Sheet")
        return SheetStore(creds, sheet_id)
    log.info("Google secrets not set, writing CSV files to data/")
    return CsvStore()


def main():
    setup_logging()
    try:
        key = get_test_key()
        session = requests.Session()
        session.auth = (key, "")              # Stripe uses the key as the username
        since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

        fresh = [charge_to_row(c) for c in fetch_charges(session, since)]
        store = pick_store()
        rows, added, updated = merge_rows(store.read_payments(), fresh)
        store.write("Payments", as_table(rows, PAYMENT_FIELDS))
        store.write("Daily totals", as_table(daily_totals(rows), TOTAL_FIELDS))

        log.info("Done: %d payments in sheet (%d new, %d updated)", len(rows), added, updated)
        return 0
    except SystemExit as err:
        log.error("%s", err)
        return 1
    except Exception:
        log.exception("Sync failed")          # full error in the log; run is marked failed
        return 1


if __name__ == "__main__":
    sys.exit(main())
