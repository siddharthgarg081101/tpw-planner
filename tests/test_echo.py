"""Local smoke tests for echo_plan. Run with: pytest tests/

Pure-compute tests — pass dicts directly to build_echo_plan. No Sheets API,
no FakeClient.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

# Make the package importable from the tests/ dir.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from echo_plan import build_echo_plan, count_working_days  # noqa: E402


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _call(
    *,
    horizon_start=date(2026, 5, 27),
    horizon_end=date(2026, 5, 31),
    orders=None,
    non_working_days=None,
    stock=None,
):
    return build_echo_plan(
        horizon_start=horizon_start,
        horizon_end=horizon_end,
        orders=orders or [],
        non_working_days=non_working_days or [],
        stock=stock or {},
    )


def _summary_section(plan):
    for blk in plan["schedule_blocks"]:
        if blk["section"].startswith("Section 4"):
            return blk
    raise AssertionError("Section 4 not found")


def _summary_rows(plan):
    return _summary_section(plan)["rows"]


def _daily_overview_rows(plan):
    for blk in plan["schedule_blocks"]:
        if blk["section"].startswith("Section 0"):
            return blk["rows"]
    raise AssertionError("Section 0 not found")


# ----------------------------------------------------------------------
# Working-day counting
# ----------------------------------------------------------------------
def test_working_days_excludes_sundays():
    # 2026-05-27 Wed ... 2026-05-31 Sun -> Wed-Sat = 4 working days.
    assert count_working_days(date(2026, 5, 27), date(2026, 5, 31), set()) == 4


def test_working_days_excludes_holidays():
    # Same window, mark 2026-05-28 (Thu) as non-working: 3 days.
    assert count_working_days(date(2026, 5, 27), date(2026, 5, 31), {date(2026, 5, 28)}) == 3


def test_working_days_single_sunday_returns_zero():
    assert count_working_days(date(2026, 5, 31), date(2026, 5, 31), set()) == 0


# ----------------------------------------------------------------------
# Orders + stock math
# ----------------------------------------------------------------------
def test_orders_and_stock_subtract_correctly():
    plan = _call(
        orders=[{"sku_id": "FG-HIL16-W-F", "sku_name": "HIL 16 W Front", "order_qty": 11745}],
        stock={"FG-HIL16-W-F": 500},
    )
    rows = _summary_rows(plan)
    assert len(rows) == 1
    row = rows[0]
    # Layout: SKU, Name, OrderQty, OpeningStock, Safety, PlanProduces, EOD, Tier, Locked, OverFlag
    assert row[0] == "FG-HIL16-W-F"
    assert row[2] == 11745
    assert row[3] == 500
    # Pending = 11745 - 500 = 11245
    assert abs(row[5] - 11245) < 0.01
    assert abs(row[6]) < 0.01
    assert row[7] == "Order"
    assert row[8] is False


def test_missing_stock_treated_as_zero():
    plan = _call(orders=[{"sku_id": "FG-X", "order_qty": 1000}])
    row = _summary_rows(plan)[0]
    assert row[3] == 0
    assert abs(row[5] - 1000) < 0.01


def test_blank_sku_rows_skipped():
    plan = _call(orders=[
        {"sku_id": "", "order_qty": 100},
        {"sku_id": None, "order_qty": 50},
        {"sku_id": "FG-X", "order_qty": 500},
    ])
    rows = _summary_rows(plan)
    assert len(rows) == 1
    assert rows[0][0] == "FG-X"


def test_clipped_at_zero_when_stock_exceeds_order():
    plan = _call(
        orders=[{"sku_id": "FG-X", "order_qty": 1000}],
        stock={"FG-X": 5000},
    )
    row = _summary_rows(plan)[0]
    assert abs(row[5]) < 0.01      # plan produces 0
    assert abs(row[6] - 4000) < 0.01  # EOD = 5000 + 0 - 1000


def test_handles_missing_optional_fields():
    """Order dict can omit name, target_dispatch_date, lock — they default safely."""
    plan = _call(orders=[{"sku_id": "FG-X", "order_qty": 1000}])
    row = _summary_rows(plan)[0]
    assert row[2] == 1000
    assert row[8] is False  # lock default


# ----------------------------------------------------------------------
# Due-date sanity warning
# ----------------------------------------------------------------------
def test_warns_when_due_date_before_horizon_start():
    plan = _call(
        horizon_start=date(2026, 5, 27),
        orders=[
            {"sku_id": "FG-X", "order_qty": 1000, "target_dispatch_date": "2026-05-20"},
            {"sku_id": "FG-Y", "order_qty": 500,  "target_dispatch_date": "2026-05-29"},
        ],
    )
    sanity = [w for w in plan["warnings"] if w[1] == "Sanity" and "FG-X" in w[2]]
    assert len(sanity) == 1
    assert "2026-05-20" in sanity[0][2]
    # FG-Y is in window, no warning for it
    assert not any("FG-Y" in w[2] for w in plan["warnings"])


def test_no_warning_when_due_date_absent():
    plan = _call(orders=[{"sku_id": "FG-X", "order_qty": 1000}])
    assert not any(w[1] == "Sanity" for w in plan["warnings"])


def test_no_warning_when_due_date_in_window():
    plan = _call(
        horizon_start=date(2026, 5, 27),
        orders=[{"sku_id": "FG-X", "order_qty": 1000, "target_dispatch_date": "2026-05-29"}],
    )
    assert not any(w[1] == "Sanity" and "FG-X" in w[2] for w in plan["warnings"])


# ----------------------------------------------------------------------
# Lock flag parsing
# ----------------------------------------------------------------------
def test_lock_flag_parses_various_forms():
    plan = _call(orders=[
        {"sku_id": "FG-A", "order_qty": 100, "lock": True},
        {"sku_id": "FG-B", "order_qty": 100, "lock": "TRUE"},
        {"sku_id": "FG-C", "order_qty": 100, "lock": "false"},
        {"sku_id": "FG-D", "order_qty": 100, "lock": ""},
        {"sku_id": "FG-E", "order_qty": 100, "lock": 1},
        {"sku_id": "FG-F", "order_qty": 100},  # missing entirely
    ])
    locks = {r[0]: r[8] for r in _summary_rows(plan)}
    assert locks["FG-A"] is True
    assert locks["FG-B"] is True
    assert locks["FG-C"] is False
    assert locks["FG-D"] is False
    assert locks["FG-E"] is True
    assert locks["FG-F"] is False


# ----------------------------------------------------------------------
# Zero-working-days edge case
# ----------------------------------------------------------------------
def test_zero_working_days_returns_warning_and_no_summary():
    plan = _call(
        horizon_start=date(2026, 5, 31),
        horizon_end=date(2026, 5, 31),  # Sunday only
        orders=[{"sku_id": "FG-X", "order_qty": 1000}],
    )
    assert len(plan["warnings"]) == 1
    assert plan["warnings"][0][1] == "Sanity"
    # All summary section should be empty / placeholder
    section_zero = next(b for b in plan["schedule_blocks"] if b["section"].startswith("Section 0"))
    assert section_zero["rows"] == []


# ----------------------------------------------------------------------
# Daily Overview
# ----------------------------------------------------------------------
def test_daily_overview_has_one_row_per_working_day():
    plan = _call(
        horizon_start=date(2026, 5, 27),
        horizon_end=date(2026, 5, 31),  # Wed Thu Fri Sat Sun -> 4 working
        orders=[{"sku_id": "FG-X", "order_qty": 400}],
    )
    rows = _daily_overview_rows(plan)
    assert len(rows) == 4
    # Each row has 10 columns matching the headers
    for r in rows:
        assert len(r) == 10
    # Dispatched column (index 7) should equal total ÷ working days = 100
    assert all(abs(r[7] - 100) < 0.01 for r in rows)


def test_daily_overview_skips_non_working_days():
    plan = _call(
        horizon_start=date(2026, 5, 27),
        horizon_end=date(2026, 5, 31),
        non_working_days=[{"date": "2026-05-28"}],  # mark Thursday as holiday
        orders=[{"sku_id": "FG-X", "order_qty": 300}],
    )
    rows = _daily_overview_rows(plan)
    # Wed Fri Sat = 3 working days (Thu and Sun excluded)
    assert len(rows) == 3
    dates = [r[0] for r in rows]
    assert "2026-05-28" not in dates


# ----------------------------------------------------------------------
# Response shape
# ----------------------------------------------------------------------
def test_response_has_six_schedule_sections():
    plan = _call(orders=[{"sku_id": "FG-X", "order_qty": 100}])
    sections = [b["section"] for b in plan["schedule_blocks"]]
    assert any(s.startswith("Section 0") for s in sections)
    assert any(s.startswith("Section 1") for s in sections)
    assert any(s.startswith("Section 2") for s in sections)
    assert any(s.startswith("Section 3") for s in sections)
    assert any(s.startswith("Section 4") for s in sections)
    assert any(s.startswith("Section 5") for s in sections)
    assert len(sections) == 6


def test_summary_string_includes_counts():
    plan = _call(orders=[
        {"sku_id": "FG-A", "order_qty": 100},
        {"sku_id": "FG-B", "order_qty": 100},
    ])
    assert "2 SKU rows" in plan["summary"]
    assert "4 working days" in plan["summary"]
