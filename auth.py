"""Shared-secret auth for the planner backend.

The Apps Script sends `X-Auth-Token: <secret>` with every plan-generation call.
The backend verifies it against the AUTH_TOKEN env var. If they don't match,
the request is rejected with 401.

This is intentionally simple — the Render service URL is non-guessable, and
this layer prevents accidental triggering by anyone who happens to find it.
For Phase 6 polish we can graduate to proper IAM-based auth.
"""

from __future__ import annotations

import os

from fastapi import HTTPException


def verify_token(provided: str | None) -> None:
    expected = os.environ.get("AUTH_TOKEN")
    if not expected:
        # Misconfiguration: bail loudly so deployment problems surface early.
        raise HTTPException(
            status_code=500,
            detail="AUTH_TOKEN env var not set on the backend.",
        )
    if provided != expected:
        raise HTTPException(
            status_code=401,
            detail="Bad or missing X-Auth-Token header.",
        )
