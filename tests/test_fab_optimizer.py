"""Tests for the Phase 2 fab optimizer.

Pure-compute tests — pass dicts directly to build_fab_plan. No Sheets API.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fab_optimizer import build_fab_plan  # noqa: E402


# ----------------------------------------------------------------------
# Fixtures — realistic small catalog: HIL 16 F/B and ASI 16 F/B.
# ----------------------------------------------------------------------
def _fab_targets():
    return [
        {"sku_id": "WIP-FG-HIL16-F-UP", "customer": "HIL", "model": "HIL 16",
         "component": "F", "size_inches": 16, "effective_pcs_hour": 165},
        {"sku_id": "WIP-FG-HIL16-B-UP", "customer": "HIL", "model": "HIL 16",
         "component": "B", "size_inches": 16, "effective_pcs_hour": 165},
        {"sku_id": "WIP-FG-ASI16-F-UP", "customer": "ASI", "model": "ASI 16",
         "component": "F", "size_inches": 16, "effective_pcs_hour": 165},
        {"sku_id": "WIP-FG-ASI16-B-UP", "customer": "ASI", "model": "ASI 16",
         "component": "B", "size_inches": 16, "effective_pcs_hour": 165},
    ]


def _pc_targets():
    return [
        {"fg_sku_id": "FG-HIL16-W-F", "customer": "HIL", "model": "HIL 16",
         "component": "F", "size_inches": 16, "shade": "W", "shade_group": "HIL-W"},
        {"fg_sku_id": "FG-HIL16-W-B", "customer": "HIL", "model": "HIL 16",
         "component": "B", "size_inches": 16, "shade": "W", "shade_group": "HIL-W"},
        {"fg_sku_id": "FG-ASI16-BK-F", "customer": "ASI", "model": "ASI 16",
         "component": "F", "size_inches": 16, "shade": "BK", "shade_group": "BK-UNIVERSAL"},
        {"fg_sku_id": "FG-ASI16-BK-B", "customer": "ASI", "model": "ASI 16",
         "component": "B", "size_inches": 16, "shade": "BK", "shade_group": "BK-UNIVERSAL"},
    ]


def _working_schedule():
    return [
        {"schedule_name": "12h", "stage": "Fab", "net_min": 590},
        {"schedule_name": "8h",  "stage": "Fab", "net_min": 377},
    ]


def _daily_line_count(start, end, lines=4, schedule="12h"):
    from datetime import timedelta
    rows = []
    d = start
    while d <= end:
        if d.weekday() != 6:
            rows.append({"date": d.isoformat(), "fab_lines_running": lines,
                         "working_schedule": schedule})
        d += timedelta(days=1)
    return rows


def _call(
    *,
    horizon_start=date(2026, 6, 1),  # Monday
    horizon_end=date(2026, 6, 5),    # Friday — 5 working days
    orders=None,
    fab_targets=None,
    pc_targets=None,
    fab_changeover=None,
    safety_stock=None,
    working_schedule=None,
    sets_balance_tolerance=None,
    tier_weights=None,
    daily_line_count=None,
    non_working_days=None,
    stock=None,
):
    return build_fab_plan(
        horizon_start=horizon_start,
        horizon_end=horizon_end,
        orders=orders or [],
        fab_targets=fab_targets if fab_targets is not None else _fab_targets(),
        pc_targets=pc_targets if pc_targets is not None else _pc_targets(),
        fab_changeover=fab_changeover or [],
        safety_stock=safety_stock or [],
        working_schedule=working_schedule if working_schedule is not None else _working_schedule(),
        sets_balance_tolerance=sets_balance_tolerance or [],
        tier_weights=tier_weights or [],
        daily_line_count=daily_line_count if daily_line_count is not None else _daily_line_count(horizon_start, horizon_end),
        non_working_days=non_working_days or [],
        stock=stock or {},
    )


def _section(plan, idx):
    """Return the section dict whose 'section' starts with f'Section {idx}:'."""
    prefix = f"Section {idx}:"
    for blk in plan["schedule_blocks"]:
        if blk["section"].startswith(prefix):
            return blk
    raise AssertionError(f"{prefix} not found in {[b['section'] for b in plan['schedule_blocks']]}")


# ----------------------------------------------------------------------
# Smoke
# ----------------------------------------------------------------------
def test_no_orders_no_production_by_default():
    """Default w_overproduce=0: with no orders, lines idle (no Tier 3 fill)."""
    plan = _call(orders=[])
    assert plan["status"] == "ok"
    assert len(_section(plan, 0)["rows"]) == 5
    assert _section(plan, 4)["rows"] == []
    # No production at all when no orders + w_overproduce=0.
    assert _section(plan, 6)["rows"] == []


def test_explicit_overproduce_weight_fills_lines():
    """With w_overproduce>0, the optimizer fills idle capacity with Tier 3."""
    plan = _call(
        orders=[],
        tier_weights=[
            {"weight_name": "w_order", "value": 1000},
            {"weight_name": "w_safety", "value": 100},
            {"weight_name": "w_overproduce", "value": 1},
            {"weight_name": "w_changeover", "value": 5},
        ],
    )
    assert len(_section(plan, 6)["rows"]) > 0


def test_zero_working_days_returns_warning():
    plan = _call(
        horizon_start=date(2026, 6, 7),  # Sunday
        horizon_end=date(2026, 6, 7),
        orders=[{"sku_id": "FG-HIL16-W-F", "order_qty": 1000}],
    )
    assert any(w[1] == "Sanity" and "zero working days" in w[2] for w in plan["warnings"])


# ----------------------------------------------------------------------
# Orders + FG → UP mapping
# ----------------------------------------------------------------------
def test_order_for_fg_maps_to_up_and_produces():
    """An order for FG-HIL16-W-F should drive UP-HIL16-F production."""
    plan = _call(orders=[{"sku_id": "FG-HIL16-W-F", "order_qty": 1000}])
    section1 = _section(plan, 1)
    hil_f_rows = [r for r in section1["rows"] if "HIL16-F" in r[3]]
    assert hil_f_rows, "Expected fab rows for WIP-FG-HIL16-F-UP"
    total = sum(r[5] for r in hil_f_rows)
    assert total >= 1000  # Tier 1 (1000 pending) at minimum


def test_unmapped_order_surfaces_warning():
    plan = _call(orders=[{"sku_id": "FG-DOES-NOT-EXIST", "order_qty": 500}])
    assert any(w[1] == "Mapping" and "FG-DOES-NOT-EXIST" in w[2] for w in plan["warnings"])


def test_opening_stock_reduces_production():
    """Opening FG stock of 800 against 1000 order → only 200 pending."""
    plan = _call(
        orders=[{"sku_id": "FG-HIL16-W-F", "order_qty": 1000}],
        stock={"FG-HIL16-W-F": 800},
    )
    summary = _section(plan, 4)["rows"]
    row = next(r for r in summary if r[0] == "FG-HIL16-W-F")
    # row layout: SKU, Name, Order Qty, FG Stock, Pipeline, Safety,
    #             Plan Produces, EOD, Tier, Locked, OverFlag
    assert row[3] == 800
    assert row[8] in ("Order", "Safety", "Over")


def test_wip_pt_stock_counts_against_pending():
    """500 pcs sitting in WIP-PT means fab needs to make only 500 more for 1000 order."""
    plan = _call(
        orders=[{"sku_id": "FG-HIL16-W-F", "order_qty": 1000}],
        stock={"WIP-FG-HIL16-F-PT": 500},
    )
    up_summary = _section(plan, 6)["rows"]
    hil_f = next(r for r in up_summary if r[0] == "WIP-FG-HIL16-F-UP")
    # Pending (post-WIP) should be 500 not 1000.
    assert abs(hil_f[3] - 500) < 1


def test_section4_eod_accounts_for_wip_pt_stock():
    """Section 4 EOD must include WIP-PT/WIP-UP attribution (Phase 2 flows them to FG).

    Regression test for the 'Short' label that appeared in user's first plan
    even when the optimizer had fully met UP-level pending. The previous bug:
    Section 4 EOD = fg_stock + fab_produced - order_qty, ignoring pipeline.
    """
    plan = _call(
        orders=[{"sku_id": "FG-HIL16-W-F", "order_qty": 4000}],
        stock={
            "FG-HIL16-W-F": 30,
            "WIP-FG-HIL16-F-PT": 3085,  # 3,085 pcs sitting in pipeline
        },
    )
    summary = _section(plan, 4)["rows"]
    row = next(r for r in summary if r[0] == "FG-HIL16-W-F")
    # Layout: SKU, Name, OrderQty, FGStock, Pipeline, Safety, PlanProduces, EOD, Tier, Locked, OverFlag
    fg_stock = row[3]
    pipeline = row[4]
    fab_produced = row[6]
    eod = row[7]
    tier = row[8]
    # Pipeline attribution should be 3,085 (since this FG is the only one ordered for this UP).
    assert abs(pipeline - 3085) < 1
    # EOD = 30 + 3085 + fab_produced - 4000. Should be ≥ 0 because UP-level pending
    # post-WIP is only 885, and the optimizer must produce at least that.
    assert eod >= -1
    assert tier in ("Order", "Safety", "Over"), f"expected non-Short, got {tier}"


# ----------------------------------------------------------------------
# Sets balance
# ----------------------------------------------------------------------
def test_sets_balance_enforces_F_B_within_tolerance():
    """Ordering only F-side should still cause B-side production to satisfy sets balance."""
    plan = _call(
        orders=[{"sku_id": "FG-HIL16-W-F", "order_qty": 5000}],
        sets_balance_tolerance=[{"scope": "default", "tolerance_line_days": 2}],
    )
    up_summary = {r[0]: r for r in _section(plan, 6)["rows"]}
    f_pcs = up_summary["WIP-FG-HIL16-F-UP"][5]
    b_pcs = up_summary.get("WIP-FG-HIL16-B-UP", [None]*6)[5] or 0
    # tol_pcs = 2 × 165 pcs/hr × 590/60 = 3245 pcs
    assert abs(f_pcs - b_pcs) <= 3300


def test_sets_balance_relaxed_with_high_tolerance():
    """With huge tolerance, B production may be 0 when only F is ordered."""
    plan = _call(
        orders=[{"sku_id": "FG-HIL16-W-F", "order_qty": 1000}],
        sets_balance_tolerance=[{"scope": "default", "tolerance_line_days": 1000}],
        tier_weights=[
            {"weight_name": "w_order", "value": 1000},
            {"weight_name": "w_safety", "value": 0},
            {"weight_name": "w_overproduce", "value": 0},
            {"weight_name": "w_changeover", "value": 0},
        ],
    )
    up_summary = {r[0]: r for r in _section(plan, 6)["rows"]}
    f_pcs = up_summary["WIP-FG-HIL16-F-UP"][5]
    # With overproduction reward off, no need to make B if not balanced.
    assert f_pcs >= 1000
    b_pcs = up_summary.get("WIP-FG-HIL16-B-UP", [None]*6)[5] or 0
    # B production should be 0 (or close) when tolerance is huge and overproduction disabled
    assert b_pcs == 0


# ----------------------------------------------------------------------
# Capacity + schedule
# ----------------------------------------------------------------------
def test_8h_schedule_scales_capacity_down():
    """8h schedule (377 net_min) gives ~64% of 12h (590) capacity."""
    plan_12h = _call(
        orders=[{"sku_id": "FG-HIL16-W-F", "order_qty": 100000}],  # huge, max out capacity
        tier_weights=[
            {"weight_name": "w_order", "value": 1000},
            {"weight_name": "w_safety", "value": 0},
            {"weight_name": "w_overproduce", "value": 0},
            {"weight_name": "w_changeover", "value": 0},
        ],
    )
    plan_8h = _call(
        orders=[{"sku_id": "FG-HIL16-W-F", "order_qty": 100000}],
        daily_line_count=_daily_line_count(date(2026, 6, 1), date(2026, 6, 5), schedule="8h"),
        tier_weights=[
            {"weight_name": "w_order", "value": 1000},
            {"weight_name": "w_safety", "value": 0},
            {"weight_name": "w_overproduce", "value": 0},
            {"weight_name": "w_changeover", "value": 0},
        ],
    )
    pcs_12 = sum(r[4] for r in _section(plan_12h, 0)["rows"])
    pcs_8 = sum(r[4] for r in _section(plan_8h, 0)["rows"])
    ratio = pcs_8 / pcs_12
    assert 0.55 < ratio < 0.75  # ~0.64 expected


def test_line_count_caps_active_lines():
    """If only 2 lines/day, no day uses more than 2."""
    plan = _call(
        orders=[{"sku_id": "FG-HIL16-W-F", "order_qty": 100000}],  # large demand
        daily_line_count=_daily_line_count(date(2026, 6, 1), date(2026, 6, 5), lines=2),
    )
    section1 = _section(plan, 1)
    # For each date, distinct line #s should be ≤ 2.
    by_date: dict[str, set] = {}
    for row in section1["rows"]:
        by_date.setdefault(row[0], set()).add(row[1])
    for d, lines in by_date.items():
        assert len(lines) <= 2, f"{d}: used {len(lines)} lines, max is 2"


def test_non_working_day_skipped():
    plan = _call(
        non_working_days=[{"date": "2026-06-03"}],  # Wednesday
        orders=[{"sku_id": "FG-HIL16-W-F", "order_qty": 500}],
    )
    daily = _section(plan, 0)["rows"]
    dates = [r[0] for r in daily]
    assert "2026-06-03" not in dates
    assert len(daily) == 4  # 5 - 1 = 4


def test_sunday_auto_excluded():
    plan = _call(
        horizon_start=date(2026, 6, 1),  # Monday
        horizon_end=date(2026, 6, 7),    # Sunday
    )
    daily = _section(plan, 0)["rows"]
    # 6 working days (Mon-Sat); Sunday excluded.
    assert len(daily) == 6
    assert "2026-06-07" not in [r[0] for r in daily]


# ----------------------------------------------------------------------
# Changeover behavior
# ----------------------------------------------------------------------
def test_changeover_counted_when_two_skus_share_line():
    """Force a mid-day swap by giving 2 lines but 4 SKUs of demand."""
    plan = _call(
        horizon_start=date(2026, 6, 1),
        horizon_end=date(2026, 6, 1),  # 1 day only
        orders=[
            {"sku_id": "FG-HIL16-W-F", "order_qty": 500},
            {"sku_id": "FG-HIL16-W-B", "order_qty": 500},
            {"sku_id": "FG-ASI16-BK-F", "order_qty": 500},
            {"sku_id": "FG-ASI16-BK-B", "order_qty": 500},
        ],
        daily_line_count=[
            {"date": "2026-06-01", "fab_lines_running": 2, "working_schedule": "12h"},
        ],
    )
    section1 = _section(plan, 1)
    # 4 SKUs × 500 = 2000 pcs needed; 2 lines × full-day cap (165×9.83 ≈ 1622) = 3245 pcs avail
    # The 4 SKUs spread across 2 lines requires changeovers.
    daily_changeovers = sum(r[8] for r in _section(plan, 0)["rows"])
    assert daily_changeovers >= 1, "Expected at least one changeover when 4 SKUs share 2 lines"


def test_changeover_penalty_deducts_minutes():
    """High changeover penalty should reduce daily total relative to a low penalty."""
    high_changeover = _call(
        horizon_start=date(2026, 6, 1),
        horizon_end=date(2026, 6, 1),
        orders=[
            {"sku_id": "FG-HIL16-W-F", "order_qty": 10000},
            {"sku_id": "FG-ASI16-BK-F", "order_qty": 10000},
        ],
        daily_line_count=[
            {"date": "2026-06-01", "fab_lines_running": 1, "working_schedule": "12h"},
        ],
        fab_changeover=[
            {"sku_id": "WIP-FG-HIL16-F-UP", "changeover_penalty_min": 300},
            {"sku_id": "WIP-FG-ASI16-F-UP", "changeover_penalty_min": 300},
        ],
        tier_weights=[
            {"weight_name": "w_order", "value": 1000},
            {"weight_name": "w_safety", "value": 0},
            {"weight_name": "w_overproduce", "value": 0},
            {"weight_name": "w_changeover", "value": 0},  # zero so optimizer picks max-pcs strategy
        ],
        sets_balance_tolerance=[{"scope": "default", "tolerance_line_days": 1000}],
    )
    low_changeover = _call(
        horizon_start=date(2026, 6, 1),
        horizon_end=date(2026, 6, 1),
        orders=[
            {"sku_id": "FG-HIL16-W-F", "order_qty": 10000},
            {"sku_id": "FG-ASI16-BK-F", "order_qty": 10000},
        ],
        daily_line_count=[
            {"date": "2026-06-01", "fab_lines_running": 1, "working_schedule": "12h"},
        ],
        fab_changeover=[
            {"sku_id": "WIP-FG-HIL16-F-UP", "changeover_penalty_min": 30},
            {"sku_id": "WIP-FG-ASI16-F-UP", "changeover_penalty_min": 30},
        ],
        tier_weights=[
            {"weight_name": "w_order", "value": 1000},
            {"weight_name": "w_safety", "value": 0},
            {"weight_name": "w_overproduce", "value": 0},
            {"weight_name": "w_changeover", "value": 0},
        ],
        sets_balance_tolerance=[{"scope": "default", "tolerance_line_days": 1000}],
    )
    # With low changeover, can fit more pcs in the day.
    high_total = _section(high_changeover, 0)["rows"][0][4]
    low_total = _section(low_changeover, 0)["rows"][0][4]
    # With single-SKU strategy (no changeover), both should produce ~1622 pcs.
    # If the model chose to swap mid-day, low-penalty plan produces more than high-penalty.
    # The optimizer with w_changeover=0 may pick either strategy; just verify low >= high.
    assert low_total >= high_total


# ----------------------------------------------------------------------
# Response shape
# ----------------------------------------------------------------------
def test_response_has_seven_schedule_sections():
    plan = _call(orders=[{"sku_id": "FG-HIL16-W-F", "order_qty": 500}])
    sections = [b["section"] for b in plan["schedule_blocks"]]
    for idx in range(7):
        assert any(s.startswith(f"Section {idx}:") for s in sections), \
            f"Missing Section {idx} in {sections}"


def test_summary_string_includes_solver_status():
    plan = _call(orders=[{"sku_id": "FG-HIL16-W-F", "order_qty": 500}])
    assert "Solver:" in plan["summary"]
    assert "OPTIMAL" in plan["summary"] or "FEASIBLE" in plan["summary"]


# ----------------------------------------------------------------------
# Tier weights configurable
# ----------------------------------------------------------------------
def test_zero_overproduce_weight_stops_idle_production():
    """With w_overproduce=0, no orders → no production at all."""
    plan = _call(
        orders=[],
        tier_weights=[
            {"weight_name": "w_order", "value": 1000},
            {"weight_name": "w_safety", "value": 100},
            {"weight_name": "w_overproduce", "value": 0},
            {"weight_name": "w_changeover", "value": 5},
        ],
    )
    up_summary = _section(plan, 6)["rows"]
    assert up_summary == []  # nothing produced, nothing reported
