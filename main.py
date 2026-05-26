"""TPW Production Planner — FastAPI backend.

Pure-compute service. No Google APIs. Apps Script (in the user's planning
workbook) reads all input tabs into a JSON payload, POSTs to this service,
receives the plan as JSON, and writes it back to the PlanOutput tabs.

Endpoints:
  GET  /healthz         — liveness probe (no auth)
  POST /generate-plan   — runs the planner. Requires X-Auth-Token header.

Phase 1 logic lives in echo_plan.py and is trivial (per-SKU even distribution).
Phase 2+ swaps that module for a real OR-Tools optimizer; the request/response
shape stays the same.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from auth import verify_token
from echo_plan import build_echo_plan

log = logging.getLogger("planner")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="TPW Planner", version="0.2.0")


# ----------------------------------------------------------------------
# I/O models
# ----------------------------------------------------------------------
class PlanRequest(BaseModel):
    """Everything the planner needs to produce a plan.

    `tunable` and `plan_input` hold lists of header-keyed dicts — one dict per
    tab row, keys normalized from sheet headers (e.g. 'SKU ID' -> 'sku_id').
    `stock` is a flat dict mapping SKU ID -> opening pcs.
    """

    horizon_start: date
    horizon_end: date
    tunable: dict[str, Any] = Field(default_factory=dict)
    plan_input: dict[str, Any] = Field(default_factory=dict)
    stock: dict[str, float] = Field(default_factory=dict)


class PlanBlock(BaseModel):
    """One section of PlanOutput_Schedule."""

    section: str
    headers: list[str]
    rows: list[list[Any]]
    placeholder: str | None = None  # shown when rows is empty


class PlanResponse(BaseModel):
    status: str
    summary: str
    schedule_blocks: list[PlanBlock]
    warnings: list[list[Any]]  # [[date, stage, description, impact], ...]


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    """Liveness probe — no auth required."""
    return {"status": "ok", "version": app.version}


@app.post("/generate-plan", response_model=PlanResponse)
def generate_plan(
    req: PlanRequest,
    x_auth_token: str | None = Header(default=None),
) -> PlanResponse:
    verify_token(x_auth_token)

    if req.horizon_end < req.horizon_start:
        raise HTTPException(
            status_code=400,
            detail="horizon_end must be on or after horizon_start",
        )

    log.info(
        "generate_plan start=%s end=%s orders=%d stock_skus=%d",
        req.horizon_start, req.horizon_end,
        len(req.plan_input.get("orders", [])),
        len(req.stock),
    )

    try:
        result = build_echo_plan(
            horizon_start=req.horizon_start,
            horizon_end=req.horizon_end,
            orders=req.plan_input.get("orders", []),
            non_working_days=req.tunable.get("non_working_days", []),
            stock=req.stock,
        )
    except Exception as e:
        log.exception("plan generation failed")
        raise HTTPException(status_code=500, detail=f"Plan generation failed: {e!s}")

    return result
