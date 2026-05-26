"""Phase 1 trivial planner.

Pure-compute. Accepts orders + opening stock + non-working-days as plain
Python data. Returns a PlanResponse-shaped dict that the FastAPI layer wraps
and returns to Apps Script. No Google API calls anywhere.

For each order with non-zero pending (= Order Qty − Opening Stock), the echo
distributes pcs evenly across working days in the horizon. Fab/PT/PC sections
of `PlanOutput_Schedule` stay empty; real per-stage scheduling arrives in
Phase 2 / Phase 3.

The function signature is intentionally narrow so Phase 2 can drop in a real
optimizer without touching `main.py`.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _to_num(x: Any) -> float:
    """Coerce a value to float. Empty / non-numeric -> 0."""
    if x is None or x == "":
        return 0.0
    try:
        return float(x)
    except (ValueError, TypeError):
        return 0.0


def _parse_date(x: Any) -> date | None:
    if not x:
        return None
    if isinstance(x, date):
        return x
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def _to_bool(x: Any) -> bool:
    """True/False/TRUE/FALSE/1/0/yes/no/empty → bool."""
    if isinstance(x, bool):
        return x
    if x is None or x == "":
        return False
    return str(x).strip().lower() in ("true", "1", "yes", "y", "t")


def count_working_days(start: date, end: date, non_working: set[date]) -> int:
    """Inclusive day count, excluding Sundays and the given non-working dates."""
    n = 0
    d = start
    while d <= end:
        if d.weekday() != 6 and d not in non_working:  # 6 = Sunday
            n += 1
        d += timedelta(days=1)
    return n


def _working_dates(start: date, end: date, non_working: set[date]) -> list[date]:
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() != 6 and d not in non_working:
            out.append(d)
        d += timedelta(days=1)
    return out


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------
def build_echo_plan(
    *,
    horizon_start: date,
    horizon_end: date,
    orders: list[dict[str, Any]],
    non_working_days: list[dict[str, Any]],
    stock: dict[str, float],
) -> dict[str, Any]:
    """Return a PlanResponse-shaped dict.

    Inputs come from Apps Script as header-keyed dicts (e.g. each order is
    {"sku_id": ..., "order_qty": ..., ...}).
    """
    warnings: list[list[Any]] = []

    # ---- 1. Working days ------------------------------------------------
    non_working = {
        d for d in (_parse_date(r.get("date")) for r in non_working_days) if d is not None
    }
    working_days = count_working_days(horizon_start, horizon_end, non_working)

    if working_days <= 0:
        warnings.append([
            "",
            "Sanity",
            "Horizon contains zero working days",
            "Plan output will be empty",
        ])
        return _empty_response(horizon_start, horizon_end, working_days, warnings)

    # ---- 2. Per-SKU summary --------------------------------------------
    summary_rows: list[list[Any]] = []
    dispatched_total = 0.0
    for r in orders:
        sku_id = str(r.get("sku_id") or "").strip()
        if not sku_id:
            continue

        name = r.get("sku_name", "")
        order_qty = _to_num(r.get("order_qty"))
        target_date = _parse_date(r.get("target_dispatch_date"))
        is_locked = _to_bool(r.get("lock"))

        # Due-date sanity check (always runs, even in Phase 1 echo).
        if target_date is not None and target_date < horizon_start:
            warnings.append([
                target_date.isoformat(),
                "Sanity",
                (f"Order for {sku_id} has Target Dispatch Date "
                 f"{target_date.isoformat()} before plan horizon start "
                 f"{horizon_start.isoformat()}"),
                "Adjust horizon start, push due date, or accept that this order won't be planned",
            ])

        opening_stock = float(stock.get(sku_id, 0.0))
        pending = max(0.0, order_qty - opening_stock)
        per_day = pending / working_days
        horizon_total = round(per_day * working_days, 1)
        eod_stock = round(opening_stock + horizon_total - order_qty, 1)
        tier_reached = "Order" if horizon_total >= pending - 0.01 else "Short"
        dispatched_total += horizon_total

        summary_rows.append([
            sku_id, name, order_qty, opening_stock,
            0,                  # Safety Target — echo doesn't use safety yet
            horizon_total,      # Plan Produces (horizon)
            eod_stock,
            tier_reached,
            is_locked,
            False,              # Overproduction Flag — echo never overproduces
        ])

    # ---- 3. Daily Overview ---------------------------------------------
    per_day_dispatch = round(dispatched_total / working_days, 1) if working_days else 0.0
    daily_rows: list[list[Any]] = []
    for d in _working_dates(horizon_start, horizon_end, non_working):
        daily_rows.append([
            d.isoformat(),
            DAY_NAMES[d.weekday()],
            "",  # Lines Running — echo doesn't read DailyLineCount
            "",  # Working Schedule
            "",  # Fab Pcs
            "",  # PT Pcs
            "",  # PC Pcs
            per_day_dispatch,
            "",  # Changeover Count
            "(Phase 1 echo: per-stage detail empty)",
        ])

    # ---- 4. Assemble response ------------------------------------------
    schedule_blocks = [
        {
            "section": "Section 0: Daily Overview",
            "headers": [
                "Date", "Day of Week", "Lines Running", "Working Schedule",
                "Fab Pcs (total)", "PT Pcs (total)", "PC Pcs (total)",
                "Dispatched Pcs (total)", "Changeover Count", "Notes",
            ],
            "rows": daily_rows,
            "placeholder": None,
        },
        {
            "section": "Section 1: Fab Schedule",
            "headers": ["Date", "Line #", "Slot", "SKU ID", "SKU Name", "Pcs", "Changeover Min", "Notes"],
            "rows": [],
            "placeholder": "(empty in Phase 1 — fab optimizer arrives in Phase 2)",
        },
        {
            "section": "Section 2: PT Schedule",
            "headers": ["Date", "UP SKU ID", "UP SKU Name", "Pcs Moved to PT", "EOD PT Stock", "PT Utilization"],
            "rows": [],
            "placeholder": "(empty in Phase 1 — PT/PC constraints arrive in Phase 3)",
        },
        {
            "section": "Section 3: PC Schedule",
            "headers": ["Date", "Shade Block #", "Shade Group", "PT SKU ID", "FG SKU ID", "Pcs", "Block Start (min)", "Block End (min)"],
            "rows": [],
            "placeholder": "(empty in Phase 1 — PC scheduling arrives in Phase 3)",
        },
        {
            "section": "Section 4: Per-SKU Horizon Summary",
            "headers": [
                "SKU ID", "SKU Name", "Order Qty", "Stock Today", "Safety Target",
                "Plan Produces (horizon)", "EOD Stock", "Tier Reached",
                "Locked?", "Overproduction Flag",
            ],
            "rows": summary_rows,
            "placeholder": None,
        },
        {
            "section": "Section 5: Dispatch Schedule",
            "headers": [
                "Date", "FG SKU ID", "SKU Name", "Pcs Dispatched",
                "Cumulative Dispatched", "Order Qty Remaining", "Notes",
            ],
            "rows": [],
            "placeholder": "(Phase 1 echo: per-day per-SKU dispatch arrives with Phase 2+)",
        },
    ]

    warning_note = f" {len(warnings)} warning(s)." if warnings else ""
    return {
        "status": "ok",
        "summary": (
            f"Echo plan generated. {len(summary_rows)} SKU rows over "
            f"{working_days} working days "
            f"({horizon_start.isoformat()} to {horizon_end.isoformat()})."
            f"{warning_note}"
        ),
        "schedule_blocks": schedule_blocks,
        "warnings": warnings,
    }


def _empty_response(
    horizon_start: date, horizon_end: date, working_days: int,
    warnings: list[list[Any]],
) -> dict[str, Any]:
    """Used when working_days == 0 — return all sections empty + warnings."""
    return {
        "status": "ok",
        "summary": (
            f"Echo plan generated. 0 SKU rows over 0 working days "
            f"({horizon_start.isoformat()} to {horizon_end.isoformat()})."
            f" {len(warnings)} warning(s)."
        ),
        "schedule_blocks": [
            {"section": "Section 0: Daily Overview", "headers": [], "rows": [],
             "placeholder": "(no working days in horizon)"},
        ],
        "warnings": warnings,
    }
