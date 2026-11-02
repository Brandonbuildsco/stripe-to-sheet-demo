# Stripe payments → Google Sheet, automatically

> **Demo / sample build. Not client work.** It runs against a Stripe **sandbox** with test payments only. No real money or real customers are involved.

Every morning this bot pulls the last 30 days of payments from Stripe and keeps a Google Sheet up to date:

| Tab | What's in it |
|---|---|
| **Payments** | One row per payment: date and time, amount, status, refunds, net, customer email, description, card brand |
| **Daily totals** | One row per day: number of payments, gross, refunds, net |

New payments are added. Payments that changed since the last run (for example, refunded) are updated in place, so the sheet never has duplicates.

## Why a business would want this

Someone on the team exports payments from Stripe and pastes them into a spreadsheet every week, then fixes the refunds by hand. This does it every day with no one touching it, and the bookkeeper always has a current sheet.

## How it's built to keep running

- **Handles big accounts:** follows Stripe's pagination (100 payments per request) until it has everything.
- **Retries:** if Stripe says "slow down" or has a temporary error, it waits (1s, 2s, 4s) and tries again.
- **Never duplicates:** each payment is matched by its Stripe ID, so re-running is always safe.
- **Test-key guard:** it refuses to run with a live (real-money) Stripe key.
- **Copies only what's needed:** card numbers never leave Stripe. The sheet only gets the card brand.
- **Fails loudly:** any error marks the run failed, GitHub emails the owner, and the log is saved for 14 days.
- **Tested:** 13 offline tests run before every sync (`tests/`).

## Run it yourself

```bash
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export STRIPE_API_KEY=sk_test_...      # your Stripe sandbox key
python seed_test_data.py 12            # optional: create 12 sample test payments
python sync.py                         # without Google secrets it writes CSVs to data/
python -m pytest -q                    # optional: run the tests
```

## Runbook (plain English)

- **Secrets it needs** (repo → Settings → Secrets and variables → Actions):
  - `STRIPE_API_KEY`: a Stripe **test** secret key (`sk_test_...`). A restricted key with read access to Charges also works for syncing.
  - `GOOGLE_SERVICE_ACCOUNT_JSON`: the whole service-account key file, pasted as text.
  - `GOOGLE_SHEET_ID`: the long ID in the sheet's web address, between `/d/` and `/edit`.
- **Share the sheet** with the service account's email as **Editor**, or the bot can't write to it.
- **Run it now:** Actions tab → *Stripe to Sheet sync* → **Run workflow**. Put a number in the box to add that many sample payments first.
- **Change the time zone:** times and daily totals are shown in Eastern time. Change `America/New_York` on the `TIMEZONE` line in `sync.py` to any zone, e.g. `America/Chicago`.
- **Change the schedule:** edit the `cron` line in `.github/workflows/sync.yml`. GitHub's free scheduler can start a run late on busy days; for exact timing, run it on a small server instead.
- **If a run fails:** open it in the Actions tab. The red step shows what broke, and the full log is under *Artifacts*. The usual causes are an expired key or the sheet not shared with the bot.

## Tech

Python · Requests · Stripe REST API · gspread (Google Sheets API) · pytest · GitHub Actions (scheduled)

---

Built by [brandonbuildsco](https://github.com/Brandonbuildsco). Want your own payments, CRM or forms flowing into a sheet? Hire me on Upwork or Fiverr: **brandonbuildsco**.
