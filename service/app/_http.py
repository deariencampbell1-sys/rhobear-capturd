"""Capped request-body helpers shared across the routes.

Starlette's ``Request.json()`` / ``Request.body()`` read the whole body into
memory with no upper bound, so a single oversized POST can exhaust the process.
(The ``max_size=`` kwarg some older patches reached for is not a real
Starlette parameter — it raises ``TypeError`` on every call.) These helpers
stream the body instead and reject with 413 the moment it crosses the
caller-supplied ceiling, so an oversized payload is never fully buffered.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, Request


async def read_body(request: Request, max_bytes: int) -> bytes:
    """Stream the request body, raising 413 once it exceeds *max_bytes*."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def read_json(request: Request, max_bytes: int) -> dict[str, Any]:
    """Read and JSON-decode a capped request body.

    413 when the body is larger than *max_bytes*; 400 when it is not valid JSON.
    An empty body decodes to ``{}`` so callers can keep using ``body.get(...)``.
    """
    raw = await read_body(request, max_bytes)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
