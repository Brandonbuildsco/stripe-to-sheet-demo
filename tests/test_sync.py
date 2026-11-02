"""
Offline tests: fake Stripe responses, no network, no keys. Run with:
    python -m pytest -q
"""

import pytest

import sync


def charge(cid, created, amount, refunded=0, status="succeeded"):
    """A minimal Stripe charge shaped like the real API response."""
    return {
        "id": cid, "created": created, "amount": amount, "currency": "usd",
        "status": status, "amount_refunded": refunded, "refunded": refunded == amount,
        "billing_details": {"email": None}, "receipt_email": "test@example.com",
        "description": "Setup fee",
        "payment_method_details": {"card": {"brand": "visa", "last4": "4242"}},
    }


OCT_31 = 1793450000   # 2026-10-31 (UTC)


def test_row_is_flat_and_in_dollars():
    row = sync.charge_to_row(charge("ch_1", OCT_31, 4900))
    assert row["amount"] == "49.00"
    assert row["net_amount"] == "49.00"
    assert row["currency"] == "USD"
    assert row["customer_email"] == "test@example.com"
    assert row["card_brand"] == "visa"
    assert row["created_utc"].startswith("2026-10-31") and row["created_utc"].endswith("UTC")


def test_card_number_is_never_copied():
    row = sync.charge_to_row(charge("ch_1", OCT_31, 4900))
    assert "4242" not in str(row.values())


def test_refund_statuses():
    assert sync.charge_to_row(charge("a", OCT_31, 4900, refunded=4900))["status"] == "refunded"
    part = sync.charge_to_row(charge("b", OCT_31, 4900, refunded=900))
    assert part["status"] == "partially refunded" and part["net_amount"] == "40.00"


def test_pagination_follows_has_more(monkeypatch):
    pages = [
        {"data": [charge("ch_3", OCT_31, 100), charge("ch_2", OCT_31, 100)], "has_more": True},
        {"data": [charge("ch_1", OCT_31, 100)], "has_more": False},
    ]
    seen = []

    def fake_get(session, path, params):
        seen.append(params.get("starting_after"))
        return pages[len(seen) - 1]

    monkeypatch.setattr(sync, "stripe_get", fake_get)
    got = sync.fetch_charges(None, sync.datetime.now(sync.timezone.utc))
    assert [c["id"] for c in got] == ["ch_3", "ch_2", "ch_1"]
    assert seen == [None, "ch_2"]          # 2nd request starts after the last ID of page 1


def test_merge_adds_new_updates_changed_keeps_old():
    old_row = sync.charge_to_row(charge("ch_old", OCT_31 - 90 * 86400, 2500))
    paid = sync.charge_to_row(charge("ch_1", OCT_31, 4900))
    # what the sheet hands back: numbers as numbers
    existing = [dict(old_row, amount=25), dict(paid, amount=49, net_amount=49, refunded_amount=0)]
    now_refunded = sync.charge_to_row(charge("ch_1", OCT_31, 4900, refunded=4900))
    new = sync.charge_to_row(charge("ch_2", OCT_31 + 60, 9900))

    rows, added, updated = sync.merge_rows(existing, [now_refunded, new])
    assert (added, updated) == (1, 1)
    assert [r["charge_id"] for r in rows] == ["ch_2", "ch_1", "ch_old"]   # newest first
    assert rows[1]["status"] == "refunded"


def test_unchanged_rows_from_sheet_are_not_counted_as_updates():
    paid = sync.charge_to_row(charge("ch_1", OCT_31, 4900))
    from_sheet = dict(paid, amount=49, net_amount=49, refunded_amount=0)
    _, added, updated = sync.merge_rows([from_sheet], [paid])
    assert (added, updated) == (0, 0)


def test_daily_totals_skip_failed_and_net_out_refunds():
    rows = [sync.charge_to_row(c) for c in [
        charge("a", OCT_31, 4900),
        charge("b", OCT_31, 10000, refunded=10000),
        charge("c", OCT_31, 7500, status="failed"),
    ]]
    [day] = sync.daily_totals(rows)
    assert day == {"date": "2026-10-31", "payments": 2, "gross": "149.00",
                   "refunds": "100.00", "net": "49.00"}


@pytest.mark.parametrize("key", ["", "sk_live_abc", "pk_test_abc"])
def test_refuses_missing_or_live_keys(monkeypatch, key):
    monkeypatch.setenv("STRIPE_API_KEY", key)
    with pytest.raises(SystemExit):
        sync.get_test_key()


def test_retries_then_succeeds(monkeypatch):
    class Resp:
        def __init__(self, code): self.status_code = code
        def raise_for_status(self): pass
        def json(self): return {"data": [], "has_more": False}

    codes = iter([429, 500, 200])

    class Session:
        def get(self, *a, **k): return Resp(next(codes))

    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    assert sync.stripe_get(Session(), "charges", {}) == {"data": [], "has_more": False}


def test_full_run_writes_csv_files(monkeypatch, tmp_path):
    """End to end without Google: a run writes both tables, a 2nd run merges cleanly."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_fake")
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.setattr(sync, "fetch_charges",
                        lambda session, since: [charge("ch_1", OCT_31, 4900), charge("ch_2", OCT_31, 2500)])
    assert sync.main() == 0
    assert sync.main() == 0                 # second run: no duplicates
    payments = (tmp_path / "data" / "payments.csv").read_text().splitlines()
    totals = (tmp_path / "data" / "daily_totals.csv").read_text().splitlines()
    assert len(payments) == 3                # header + 2 payments
    assert totals[1] == "2026-10-31,2,74.00,0.00,74.00"
