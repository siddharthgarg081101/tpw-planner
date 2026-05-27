"""Phase 2 fabrication optimizer.

CP-SAT MILP over the fab stage. PT and PC are assumed unconstrained (Phase 3
adds those). Reads orders, opening stock, tunables; returns per-(date, line)
SKU assignments + pcs, daily totals, and per-SKU horizon summary.

Decision space:
  produces[d, l, sku] in {0,1}    — does line l produce sku on day d?
  pcs[d, l, sku]      in Z>=0     — pcs of sku produced on (d, l)
  from_sku[d, l, sku] in {0,1}    — sku is the "switched-from" SKU on (d,l)
  multi[d, l]         in {0,1}    — does (d, l) run 2 SKUs (= 1 changeover)?
  pcs_tier1/2/3[sku]  in Z>=0     — UP pcs allocated to orders/safety/overproduce

Objective:
  max  w1*Σtier1 + w2*Σtier2 + w3*Σtier3 − w_changeover × Σmulti

Capacity (per d, l, scaled ×1000 for integer arithmetic):
  Σ pcs[d,l,sku] × time_per_pc_scaled[sku]
    + Σ from_sku[d,l,sku] × changeover_min[sku] × 1000
    ≤ line_active[d,l] × net_fab_min[d] × 1000

Solver budget: 60 sec. If only FEASIBLE (not OPTIMAL) is found, a warning is
emitted but the best solution so far is returned.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ortools.sat.python import cp_model

# Scale factor for integer arithmetic on fractional minutes-per-pc.
# At SCALE=1000 the rounding error is ~0.1% per pc — negligible vs. plan horizons.
SCALE = 1000

# Default tier weights — used only if Tunable_TierWeights is empty/missing.
DEFAULT_WEIGHTS = {
    "w_order": 1000,
    "w_safety": 100,
    "w_overproduce": 1,
    "w_changeover": 5,
}

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

SOLVE_TIME_LIMIT_SEC = 60


# ----------------------------------------------------------------------
# Helpers (coercion + normalization)
# ----------------------------------------------------------------------
def _to_num(x: Any, default: float = 0.0) -> float:
    if x is None or x == "":
        return default
    try:
        return float(x)
    except (ValueError, TypeError):
        return default


def _to_int(x: Any, default: int = 0) -> int:
    return int(round(_to_num(x, default)))


def _to_bool(x: Any) -> bool:
    if isinstance(x, bool):
        return x
    if x is None or x == "":
        return False
    return str(x).strip().lower() in ("true", "1", "yes", "y", "t")


def _parse_date(x: Any) -> date | None:
    if not x:
        return None
    if isinstance(x, date):
        return x
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


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
def build_fab_plan(
    *,
    horizon_start: date,
    horizon_end: date,
    orders: list[dict[str, Any]],
    fab_targets: list[dict[str, Any]],
    pc_targets: list[dict[str, Any]],
    fab_changeover: list[dict[str, Any]],
    safety_stock: list[dict[str, Any]],
    working_schedule: list[dict[str, Any]],
    sets_balance_tolerance: list[dict[str, Any]],
    tier_weights: list[dict[str, Any]],
    daily_line_count: list[dict[str, Any]],
    non_working_days: list[dict[str, Any]],
    stock: dict[str, float],
) -> dict[str, Any]:
    warnings: list[list[Any]] = []

    # ---- 1. Horizon -----------------------------------------------------
    non_working = {
        d for d in (_parse_date(r.get("date")) for r in non_working_days) if d is not None
    }
    dates = _working_dates(horizon_start, horizon_end, non_working)
    if not dates:
        warnings.append([
            "", "Sanity", "Horizon contains zero working days",
            "Plan output will be empty",
        ])
        return _empty_response(horizon_start, horizon_end, warnings)

    # ---- 2. Tier weights -----------------------------------------------
    weights = dict(DEFAULT_WEIGHTS)
    for r in tier_weights:
        name = str(r.get("weight_name") or "").strip().lower()
        if name in weights:
            weights[name] = _to_int(r.get("value"), weights[name])

    # ---- 3. Fab SKU catalog --------------------------------------------
    # Each fab row: sku_id, customer, model, component, size_inches, effective_pcs_hour
    fab_skus: dict[str, dict[str, Any]] = {}
    for r in fab_targets:
        sku = str(r.get("sku_id") or "").strip()
        if not sku:
            continue
        rate = _to_int(r.get("effective_pcs_hour"))
        if rate <= 0:
            warnings.append([
                "", "Sanity",
                f"Fab SKU {sku} has non-positive effective Pcs/Hour; excluded",
                "Set a positive rate in Tunable_FabTargets",
            ])
            continue
        fab_skus[sku] = {
            "sku_id": sku,
            "customer": str(r.get("customer") or "").strip(),
            "model": str(r.get("model") or "").strip(),
            "component": str(r.get("component") or "").strip(),
            "size_inches": _to_int(r.get("size_inches")),
            "rate": rate,
            "time_per_pc_scaled": max(1, round(60 * SCALE / rate)),
        }

    if not fab_skus:
        warnings.append([
            "", "Sanity", "No fab SKUs found in Tunable_FabTargets",
            "Plan output will be empty",
        ])
        return _empty_response(horizon_start, horizon_end, warnings)

    # ---- 4. Changeover penalties (default 120 if missing) --------------
    changeover_min: dict[str, int] = {sku: 120 for sku in fab_skus}
    for r in fab_changeover:
        sku = str(r.get("sku_id") or "").strip()
        if sku in changeover_min:
            changeover_min[sku] = _to_int(r.get("changeover_penalty_min"), 120)

    # ---- 5. FG → UP mapping via (customer, model, component) -----------
    fg_to_up: dict[str, str] = {}
    fg_meta: dict[str, dict[str, Any]] = {}
    up_by_cmc: dict[tuple[str, str, str], str] = {
        (m["customer"], m["model"], m["component"]): sku for sku, m in fab_skus.items()
    }
    for r in pc_targets:
        fg = str(r.get("fg_sku_id") or "").strip()
        if not fg:
            continue
        cust = str(r.get("customer") or "").strip()
        model = str(r.get("model") or "").strip()
        comp = str(r.get("component") or "").strip()
        up = up_by_cmc.get((cust, model, comp))
        if up:
            fg_to_up[fg] = up
        fg_meta[fg] = {
            "customer": cust, "model": model, "component": comp,
            "size_inches": _to_int(r.get("size_inches")),
            "shade": str(r.get("shade") or "").strip(),
            "shade_group": str(r.get("shade_group") or "").strip(),
        }

    # ---- 6. Working schedule lookup (stage=Fab) ------------------------
    fab_net_min: dict[str, int] = {}
    for r in working_schedule:
        if str(r.get("stage") or "").strip().lower() != "fab":
            continue
        name = str(r.get("schedule_name") or "").strip()
        fab_net_min[name] = _to_int(r.get("net_min"))
    # Fall back to 590 / 377 if missing
    fab_net_min.setdefault("12h", 590)
    fab_net_min.setdefault("8h", 377)

    # ---- 7. Per-day capacity + lines available --------------------------
    daily_cfg: dict[date, dict[str, Any]] = {}
    for r in daily_line_count:
        d = _parse_date(r.get("date"))
        if d is None or d not in dates:
            continue
        sched = str(r.get("working_schedule") or "12h").strip()
        net_min = fab_net_min.get(sched, fab_net_min.get("12h", 590))
        daily_cfg[d] = {
            "lines_running": max(0, _to_int(r.get("fab_lines_running"))),
            "schedule": sched,
            "net_min": net_min,
        }
    # Default for any working day without a DailyLineCount row: 8 lines, 12h.
    for d in dates:
        if d not in daily_cfg:
            sched = "12h"
            daily_cfg[d] = {
                "lines_running": 8,
                "schedule": sched,
                "net_min": fab_net_min.get(sched, 590),
            }
            warnings.append([
                d.isoformat(), "Sanity",
                f"No PlanInput_DailyLineCount row for {d.isoformat()}; defaulting to 8 lines, 12h",
                "Add a row to PlanInput_DailyLineCount to override",
            ])

    # ---- 8. Per-UP demand (Tier 1) + safety (Tier 2) --------------------
    # Aggregate FG orders to UP level. Subtract pipeline stock that flows back to this UP.
    up_pending: dict[str, float] = {sku: 0.0 for sku in fab_skus}
    up_safety: dict[str, float] = {sku: 0.0 for sku in fab_skus}
    order_rows: list[dict[str, Any]] = []  # for Section 4 reporting
    unmapped_orders: list[str] = []

    safety_lookup = {
        str(r.get("sku_id") or "").strip(): _to_num(r.get("safety_stock_pcs"))
        for r in safety_stock if str(r.get("sku_id") or "").strip()
    }

    for r in orders:
        sku = str(r.get("sku_id") or "").strip()
        if not sku:
            continue
        qty = _to_num(r.get("order_qty"))
        opening = float(stock.get(sku, 0.0))
        pending = max(0.0, qty - opening)
        target_d = _parse_date(r.get("target_dispatch_date"))
        is_locked = _to_bool(r.get("lock"))
        if target_d is not None and target_d < horizon_start:
            warnings.append([
                target_d.isoformat(), "Sanity",
                (f"Order for {sku} has Target Dispatch Date {target_d.isoformat()} "
                 f"before plan horizon start {horizon_start.isoformat()}"),
                "Adjust horizon start, push due date, or accept that this order won't be planned",
            ])

        up = fg_to_up.get(sku)
        if up is None:
            # Maybe user typed the UP SKU directly (e.g., HIL v3 direct-to-dispatch)
            if sku in fab_skus:
                up = sku
            else:
                unmapped_orders.append(sku)
        order_rows.append({
            "sku_id": sku,
            "sku_name": r.get("sku_name", ""),
            "order_qty": qty,
            "opening_stock": opening,
            "pending": pending,
            "up": up,
            "is_locked": is_locked,
            "safety": safety_lookup.get(sku, 0.0),
        })
        if up is not None:
            up_pending[up] += pending
            up_safety[up] += safety_lookup.get(sku, 0.0)

    # Subtract upstream WIP for each UP (WIP-UP + WIP-PT stock counts against pending).
    for up_sku, meta in fab_skus.items():
        wip_up_stock = float(stock.get(up_sku, 0.0))
        pt_sku = up_sku.replace("-UP", "-PT")
        wip_pt_stock = float(stock.get(pt_sku, 0.0))
        slack = wip_up_stock + wip_pt_stock
        # Apply slack to pending first, then safety.
        if slack >= up_pending[up_sku]:
            slack -= up_pending[up_sku]
            up_pending[up_sku] = 0.0
        else:
            up_pending[up_sku] -= slack
            slack = 0.0
        if slack >= up_safety[up_sku]:
            up_safety[up_sku] = 0.0
        else:
            up_safety[up_sku] -= slack

    if unmapped_orders:
        unique = sorted(set(unmapped_orders))
        warnings.append([
            "", "Mapping",
            f"Orders reference SKUs not found in Tunable_PCTargets or Tunable_FabTargets: {', '.join(unique)}",
            "Add these SKUs to PCTargets (and ensure their model+component matches a Fab SKU)",
        ])

    # ---- 9. Sets-balance tolerance --------------------------------------
    default_tol = 2
    model_tol: dict[str, int] = {}
    for r in sets_balance_tolerance:
        scope = str(r.get("scope") or "").strip()
        tol = _to_int(r.get("tolerance_line_days"), default_tol)
        if scope.lower() == "default":
            default_tol = tol
        elif scope:
            model_tol[scope] = tol

    # ---- 10. Build the CP-SAT model ------------------------------------
    model = cp_model.CpModel()
    sku_list = list(fab_skus.keys())
    line_indices = list(range(max(cfg["lines_running"] for cfg in daily_cfg.values()) or 1))

    produces: dict[tuple, cp_model.IntVar] = {}
    pcs: dict[tuple, cp_model.IntVar] = {}
    from_sku: dict[tuple, cp_model.IntVar] = {}
    multi: dict[tuple, cp_model.IntVar] = {}
    line_active: dict[tuple, cp_model.IntVar] = {}

    # Upper bound for any single (d, l, sku) pcs — full day at max rate.
    MAX_PCS_PER_DL = max(
        m["rate"] * cfg["net_min"] // 60 + 1
        for cfg in daily_cfg.values() for m in fab_skus.values()
    )

    for d in dates:
        cfg = daily_cfg[d]
        max_lines = cfg["lines_running"]
        for l in line_indices:
            line_used = l < max_lines
            la = model.NewBoolVar(f"la_{d}_{l}")
            line_active[(d, l)] = la
            if not line_used:
                # Hard-disable line: cannot be active.
                model.Add(la == 0)
            mu = model.NewBoolVar(f"mu_{d}_{l}")
            multi[(d, l)] = mu

            produces_in_dl = []
            from_in_dl = []
            pcs_terms = []
            changeover_terms = []
            for sku in sku_list:
                p = model.NewBoolVar(f"prod_{d}_{l}_{sku}")
                produces[(d, l, sku)] = p
                produces_in_dl.append(p)

                pc = model.NewIntVar(0, MAX_PCS_PER_DL, f"pcs_{d}_{l}_{sku}")
                pcs[(d, l, sku)] = pc

                fr = model.NewBoolVar(f"from_{d}_{l}_{sku}")
                from_sku[(d, l, sku)] = fr
                from_in_dl.append(fr)

                # pcs only flows if produces is on; capped at full-day cap for sku.
                full_day_cap = fab_skus[sku]["rate"] * cfg["net_min"] // 60
                model.Add(pc <= p * full_day_cap)
                # from_sku implies produces is on for this sku.
                model.Add(fr <= p)
                # from_sku only when multi.
                model.Add(fr <= mu)

                pcs_terms.append(pc * fab_skus[sku]["time_per_pc_scaled"])
                changeover_terms.append(fr * changeover_min[sku] * SCALE)

            # produces_in_dl ≤ 2 SKUs per (d, l).
            model.Add(sum(produces_in_dl) <= 2)
            # produces requires line active.
            for p in produces_in_dl:
                model.Add(p <= la)
            # multi = (produces_count ≥ 2). With ≤ 2 constraint, multi = (sum==2).
            model.Add(sum(produces_in_dl) >= 2 * mu)
            model.Add(sum(produces_in_dl) <= 1 + mu)  # = 2 → mu must be 1
            # Exactly one from_sku when multi, zero when not.
            model.Add(sum(from_in_dl) == mu)
            # Line active ↔ at least one produces.
            model.Add(la <= sum(produces_in_dl))

            # Capacity constraint (scaled).
            model.Add(
                sum(pcs_terms) + sum(changeover_terms)
                <= la * cfg["net_min"] * SCALE
            )

    # ---- 11. Tier-cap demand per UP ------------------------------------
    pcs_tier1: dict[str, cp_model.IntVar] = {}
    pcs_tier2: dict[str, cp_model.IntVar] = {}
    pcs_tier3: dict[str, cp_model.IntVar] = {}
    for sku in sku_list:
        total_cap = int(up_pending[sku] + up_safety[sku]) + MAX_PCS_PER_DL * len(dates) * len(line_indices)
        t1 = model.NewIntVar(0, int(up_pending[sku]), f"t1_{sku}")
        t2 = model.NewIntVar(0, int(up_safety[sku]), f"t2_{sku}")
        t3 = model.NewIntVar(0, total_cap, f"t3_{sku}")
        pcs_tier1[sku] = t1
        pcs_tier2[sku] = t2
        pcs_tier3[sku] = t3
        # Total UP production = t1 + t2 + t3.
        total_pcs_sku = sum(
            pcs[(d, l, sku)] for d in dates for l in line_indices
        )
        model.Add(total_pcs_sku == t1 + t2 + t3)

    # ---- 12. Sets-balance constraint -----------------------------------
    by_model: dict[tuple[str, str], dict[str, list[str]]] = {}
    for sku, m in fab_skus.items():
        key = (m["customer"], m["model"])
        by_model.setdefault(key, {}).setdefault(m["component"], []).append(sku)

    for (cust, mod), comps in by_model.items():
        sku_pairs = [c for c in ("F", "B", "SB") if c in comps]
        if len(sku_pairs) < 2:
            continue
        tol_days = model_tol.get(mod, default_tol)
        # Reference: 12h Fab net_min × largest rate among components (in pcs).
        ref_rate = max(
            fab_skus[s]["rate"] for c in sku_pairs for s in comps[c]
        )
        tol_pcs = tol_days * ref_rate * fab_net_min.get("12h", 590) // 60

        totals: dict[str, cp_model.LinearExprT] = {}
        for c in sku_pairs:
            totals[c] = sum(
                pcs[(d, l, sku)]
                for d in dates for l in line_indices for sku in comps[c]
            )
        # Pairwise balance |F − B| ≤ tol; |F − SB| ≤ tol if SB present.
        for i in range(len(sku_pairs)):
            for j in range(i + 1, len(sku_pairs)):
                a, b = totals[sku_pairs[i]], totals[sku_pairs[j]]
                model.Add(a - b <= tol_pcs)
                model.Add(b - a <= tol_pcs)

    # ---- 13. Objective --------------------------------------------------
    model.Maximize(
        weights["w_order"]       * sum(pcs_tier1[s] for s in sku_list)
        + weights["w_safety"]    * sum(pcs_tier2[s] for s in sku_list)
        + weights["w_overproduce"] * sum(pcs_tier3[s] for s in sku_list)
        - weights["w_changeover"] * sum(multi[(d, l)] for d in dates for l in line_indices)
    )

    # ---- 14. Solve ------------------------------------------------------
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = SOLVE_TIME_LIMIT_SEC
    # Render free tier is single-CPU; explicit worker count keeps memory predictable.
    solver.parameters.num_search_workers = 1
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        warnings.append([
            "", "Solver",
            f"Solver returned status {solver.StatusName(status)} — no feasible plan found",
            "Check that horizon, line counts, and order qty are consistent",
        ])
        return _empty_response(horizon_start, horizon_end, warnings)

    if status == cp_model.FEASIBLE:
        warnings.append([
            "", "Solver",
            f"Solver hit {SOLVE_TIME_LIMIT_SEC}s time limit before proving optimality",
            "Best feasible plan returned. Tighten horizon or simplify orders to get optimal.",
        ])

    # ---- 15. Read solution + build outputs ------------------------------
    return _build_output(
        solver=solver,
        dates=dates,
        line_indices=line_indices,
        fab_skus=fab_skus,
        sku_list=sku_list,
        daily_cfg=daily_cfg,
        produces=produces,
        pcs=pcs,
        from_sku=from_sku,
        multi=multi,
        line_active=line_active,
        pcs_tier1=pcs_tier1,
        pcs_tier2=pcs_tier2,
        pcs_tier3=pcs_tier3,
        up_pending=up_pending,
        up_safety=up_safety,
        order_rows=order_rows,
        fg_to_up=fg_to_up,
        changeover_min=changeover_min,
        horizon_start=horizon_start,
        horizon_end=horizon_end,
        warnings=warnings,
    )


# ----------------------------------------------------------------------
# Output assembly
# ----------------------------------------------------------------------
def _build_output(
    *, solver, dates, line_indices, fab_skus, sku_list, daily_cfg,
    produces, pcs, from_sku, multi, line_active,
    pcs_tier1, pcs_tier2, pcs_tier3,
    up_pending, up_safety, order_rows, fg_to_up,
    changeover_min,
    horizon_start, horizon_end, warnings,
) -> dict[str, Any]:

    # ----- Section 1: Fab Schedule (one row per (d, l, slot, sku)) --------
    fab_rows: list[list[Any]] = []
    for d in dates:
        for l in line_indices:
            if solver.Value(line_active[(d, l)]) == 0:
                continue
            # Determine which SKUs ran on this (d, l).
            skus_on_dl = [
                s for s in sku_list if solver.Value(produces[(d, l, s)]) == 1
            ]
            if len(skus_on_dl) == 1:
                s = skus_on_dl[0]
                fab_rows.append([
                    d.isoformat(), l + 1, "Full", s, fab_skus[s]["model"],
                    solver.Value(pcs[(d, l, s)]), 0, "",
                ])
            elif len(skus_on_dl) == 2:
                # The from_sku is the one switched FROM (AM slot). The other is PM.
                from_s = next(
                    (s for s in skus_on_dl if solver.Value(from_sku[(d, l, s)]) == 1),
                    skus_on_dl[0],
                )
                to_s = next(s for s in skus_on_dl if s != from_s)
                fab_rows.append([
                    d.isoformat(), l + 1, "AM", from_s, fab_skus[from_s]["model"],
                    solver.Value(pcs[(d, l, from_s)]), 0, "switched from at midday",
                ])
                fab_rows.append([
                    d.isoformat(), l + 1, "PM", to_s, fab_skus[to_s]["model"],
                    solver.Value(pcs[(d, l, to_s)]),
                    changeover_min[from_s],
                    f"changeover from {from_s} ({changeover_min[from_s]} min lost)",
                ])

    # ----- Section 0: Daily Overview --------------------------------------
    daily_rows: list[list[Any]] = []
    for d in dates:
        cfg = daily_cfg[d]
        active_lines = sum(solver.Value(line_active[(d, l)]) for l in line_indices)
        day_fab_pcs = sum(
            solver.Value(pcs[(d, l, s)])
            for l in line_indices for s in sku_list
        )
        day_changeovers = sum(solver.Value(multi[(d, l)]) for l in line_indices)
        daily_rows.append([
            d.isoformat(),
            DAY_NAMES[d.weekday()],
            f"{active_lines}/{cfg['lines_running']}",
            cfg["schedule"],
            day_fab_pcs,
            "",  # PT — Phase 3
            "",  # PC — Phase 3
            "",  # Dispatched — Phase 4
            day_changeovers,
            "",
        ])

    # ----- Section 4: Per-SKU Horizon Summary (per FG order) --------------
    # Pre-aggregate UP production + order share per UP.
    up_total_produced: dict[str, int] = {
        sku: sum(solver.Value(pcs[(d, l, sku)]) for d in dates for l in line_indices)
        for sku in sku_list
    }
    up_order_qty_total: dict[str, float] = {}
    for r in order_rows:
        up = r["up"]
        if up:
            up_order_qty_total[up] = up_order_qty_total.get(up, 0.0) + r["order_qty"]

    summary_rows: list[list[Any]] = []
    for r in order_rows:
        up = r["up"]
        if up is None:
            attributed = 0.0
            eod = r["opening_stock"] - r["order_qty"]
            tier = "Unmapped"
        else:
            total_q = up_order_qty_total.get(up, 0.0)
            share = (r["order_qty"] / total_q) if total_q > 0 else 0.0
            attributed = up_total_produced[up] * share
            eod = r["opening_stock"] + attributed - r["order_qty"]
            if attributed + r["opening_stock"] >= r["order_qty"] + r["safety"] - 0.01:
                tier = "Safety"
            elif attributed + r["opening_stock"] >= r["order_qty"] - 0.01:
                tier = "Order"
            else:
                tier = "Short"
        # Overproduction = UP produced more than (pending + safety) and this FG was credited some of it.
        if up is not None:
            overproduction = up_total_produced[up] > (up_pending[up] + up_safety[up]) + 0.01
        else:
            overproduction = False
        summary_rows.append([
            r["sku_id"], r["sku_name"], r["order_qty"], r["opening_stock"],
            r["safety"], round(attributed, 1), round(eod, 1),
            tier, r["is_locked"], overproduction,
        ])

    # ----- Section 6 (UP-level summary) -----------------------------------
    # Included as extra context so the user can see fab-level production directly.
    up_summary_rows: list[list[Any]] = []
    for sku in sku_list:
        produced = up_total_produced[sku]
        if produced == 0 and up_pending[sku] == 0 and up_safety[sku] == 0:
            continue  # skip dead rows
        up_summary_rows.append([
            sku, fab_skus[sku]["model"], fab_skus[sku]["component"],
            round(up_pending[sku], 1),
            round(up_safety[sku], 1),
            produced,
            solver.Value(pcs_tier1[sku]),
            solver.Value(pcs_tier2[sku]),
            solver.Value(pcs_tier3[sku]),
        ])

    schedule_blocks = [
        {
            "section": "Section 0: Daily Overview",
            "headers": [
                "Date", "Day of Week", "Lines Used / Available", "Working Schedule",
                "Fab Pcs (total)", "PT Pcs (total)", "PC Pcs (total)",
                "Dispatched Pcs (total)", "Changeover Count", "Notes",
            ],
            "rows": daily_rows,
            "placeholder": None,
        },
        {
            "section": "Section 1: Fab Schedule",
            "headers": [
                "Date", "Line #", "Slot", "UP SKU ID", "Model",
                "Pcs", "Changeover Min Lost", "Notes",
            ],
            "rows": fab_rows,
            "placeholder": "(no fab activity scheduled — check that orders create demand and lines are running)",
        },
        {
            "section": "Section 2: PT Schedule",
            "headers": ["Date", "UP SKU ID", "Pcs Moved to PT", "EOD PT Stock", "PT Utilization"],
            "rows": [],
            "placeholder": "(empty in Phase 2 — PT scheduling arrives in Phase 3)",
        },
        {
            "section": "Section 3: PC Schedule",
            "headers": ["Date", "Shade Block #", "Shade Group", "PT SKU ID", "FG SKU ID",
                        "Pcs", "Block Start (min)", "Block End (min)"],
            "rows": [],
            "placeholder": "(empty in Phase 2 — PC scheduling arrives in Phase 3)",
        },
        {
            "section": "Section 4: Per-SKU Horizon Summary (FG orders)",
            "headers": [
                "SKU ID", "SKU Name", "Order Qty", "Stock Today", "Safety Target",
                "Plan Produces (attributed)", "EOD Stock", "Tier Reached",
                "Locked?", "Overproduction Flag",
            ],
            "rows": summary_rows,
            "placeholder": "(no orders in PlanInput_Orders)",
        },
        {
            "section": "Section 5: Dispatch Schedule",
            "headers": [
                "Date", "FG SKU ID", "Pcs Dispatched",
                "Cumulative Dispatched", "Order Qty Remaining", "Notes",
            ],
            "rows": [],
            "placeholder": "(empty in Phase 2 — dispatch scheduling arrives in Phase 4)",
        },
        {
            "section": "Section 6: UP-level Production Summary (fab-stage detail)",
            "headers": [
                "UP SKU ID", "Model", "Component",
                "Pending (post-WIP)", "Safety (post-WIP)",
                "Pcs Produced", "Tier 1 (Order)", "Tier 2 (Safety)", "Tier 3 (Over)",
            ],
            "rows": up_summary_rows,
            "placeholder": "(no UP-level activity)",
        },
    ]

    total_pcs = sum(up_total_produced.values())
    total_changeovers = sum(
        solver.Value(multi[(d, l)]) for d in dates for l in line_indices
    )
    warning_note = f" {len(warnings)} warning(s)." if warnings else ""
    return {
        "status": "ok",
        "summary": (
            f"Fab plan: {total_pcs:,} pcs across {len(dates)} working days "
            f"({horizon_start.isoformat()} to {horizon_end.isoformat()}), "
            f"{total_changeovers} mid-day changeover(s). "
            f"Solver: {solver.StatusName()} in {solver.WallTime():.1f}s."
            f"{warning_note}"
        ),
        "schedule_blocks": schedule_blocks,
        "warnings": warnings,
    }


def _empty_response(
    horizon_start: date, horizon_end: date, warnings: list[list[Any]],
) -> dict[str, Any]:
    return {
        "status": "ok",
        "summary": (
            f"Fab plan: 0 pcs ({horizon_start.isoformat()} to {horizon_end.isoformat()})."
            f" {len(warnings)} warning(s)."
        ),
        "schedule_blocks": [
            {"section": "Section 0: Daily Overview", "headers": [], "rows": [],
             "placeholder": "(no working days in horizon or no fab SKUs configured)"},
        ],
        "warnings": warnings,
    }
