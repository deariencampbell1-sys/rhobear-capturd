"""Captur'd hosted service — the shippable app.

Full lifecycle: auth → load-protection gates → render → deliver. Serves its
own frontend. Reuses render_worker (cost cap) + the Director rig. Everything works
end-to-end today except the owner's payment credentials (see billing.py / config.py).
"""
from __future__ import annotations

import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

APP_DIR = Path(__file__).resolve().parent
SERVICE_DIR = APP_DIR.parent
sys.path.insert(0, str(SERVICE_DIR))

from app import auth, billing, config, store            # noqa: E402
from app._http import read_json                          # noqa: E402
from app.auth import current_user, require_user          # noqa: E402
from capturd._net import is_private_ip                   # noqa: E402
from capturd.engine.contract import engine_contract      # noqa: E402
from render_worker import CostCap, JobSpec, run_job       # noqa: E402

# the Director scout lives in the rig; add it to path. Same env var + default as
# render_worker.RIG so enabling the rig turns on both the scout (here) and
# film.py (there) — defaults to the live-box layout /opt/capturd-rig.
RIG = Path(config._env("CAPTURD_RIG_DIR", "/opt/capturd-rig"))
if RIG.is_dir():
    sys.path.insert(0, str(RIG))
try:
    from scout import build_shot                          # noqa: E402
except Exception:
    build_shot = None


# template -> camera style (the look; AI-scout tightens the shot later)
TEMPLATE_STYLE = {
    "saas-walkthrough": "snappy", "ux-showcase": "cinematic",
    "tutorial-longform": "professional", "feature-spotlight": "smooth",
    "social-teaser": "snappy", "login-flow": "snappy",
}


def shot_from_template(url: str, template: str, name: str, brief: str = "") -> dict:
    """A universal, always-works scroll-tour shot for any URL. The Director
    brain (scout→plan) replaces this with a tight per-app shot later; this
    guarantees a real branded video today. A director's brief becomes the
    demo's opening line — the narrator speaks the user's own pitch."""
    style = TEMPLATE_STYLE.get(template, "snappy")
    opening = (brief or "").strip() or "A look at the product"
    return {
        "name": name, "start_url": url, "prewait": 16, "style": style,
        "steps": [
            {"action": "scroll", "value": "down", "note": opening[:280], "wait": 2},
            {"action": "scroll", "value": "down", "note": "", "wait": 2},
            {"action": "scroll", "value": "top", "note": "Back to the top", "wait": 2},
        ],
        "export": ["mp4"],
    }


# ── SSRF guard ─────────────────────────────────────────────────────────────
# A submitted URL drives a server-side render (Playwright via film.py). Without
# this, a user can point a generation at an internal address — 169.254.169.254
# (cloud metadata), 127.0.0.1 (the service itself), or an RFC 1918 host on the
# box's private network — and read whatever the render returns. We resolve the
# host and reject private/internal ranges at submit time.
#
# The IP classification is the ONE shared copy in capturd._net (imported above),
# used by both this service and the MCP server's demo.record, so the two
# surfaces can't drift — the MCP copy used to be a hand-rolled one that failed
# open on malformed input and missed IPv6-mapped IPv4 metadata addresses.


def _reject_private_url(url: str) -> None:
    """Raise HTTPException if *url* resolves to a private/internal IP."""
    host = urlparse(url).hostname
    if not host:
        raise HTTPException(status_code=400, detail="could not parse host from url")
    try:
        addrinfo = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise HTTPException(status_code=400, detail=f"hostname not found: {exc}") from exc
    for _family, _type, _proto, _canon, sockaddr in addrinfo:
        ip = sockaddr[0]
        if is_private_ip(ip):
            raise HTTPException(
                status_code=403,
                detail=f"url resolves to a private/internal IP ({ip}) — not allowed",
            )


# ── engine contract (W3-06d) ─────────────────────────────────────────────
# The one implementation lives in ``capturd.engine.contract``; the manifest is
# read only through ``capturd.engine.load_manifest`` there. The public answer
# carries version metadata only — never the server's filesystem path, which
# stays on the operator-scoped MCP tool.


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init()
    config.ensure_dirs()
    yield


app = FastAPI(title="Captur'd", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(billing.router)


@app.get("/healthz")
async def healthz():
    # The contract carries its own success/failure shape (ok, error, detail),
    # so a manifest that fails validation is reported in the field instead of
    # taking the health answer down. The contract is the shared implementation
    # and never carries the server's filesystem path — that lives on the
    # operator-scoped MCP tool, sourced from the loader's own manifest path.
    return {"ok": True, **config.status(), "engine_contract": engine_contract()}


@app.get("/api/me")
async def me(request: Request):
    u = current_user(request)
    if not u:
        return {"signed_in": False, "config": config.status()}
    gens = store.usage_count(u["id"], "generation")
    return {
        "signed_in": True, "email": u["email"], "plan": u["plan"],
        "usage": {"generations": gens},
        "mcp_endpoint": f"{config.BASE_URL}/mcp/{store.mcp_token_for(u['id'])}",
        "config": config.status(),
    }


def _run_generation(job_id: str, uid: str, url: str, template: str,
                    name: str, spec_kwargs: dict) -> None:
    store.record_job(job_id, uid, "walk", "running")
    # Director scout → a tight per-app shot; fall back to the generic tour.
    shot = None
    brief = spec_kwargs.pop("brief", "")
    if build_shot is not None:
        try:
            shot = build_shot(url, template, name)
        except Exception:
            shot = None
    if not shot:
        shot = shot_from_template(url, template, name, brief)
    spec = JobSpec(shot=shot, job_id=job_id, **spec_kwargs)
    res = run_job(spec, config.JOBS_DIR)
    store.record_job(job_id, uid, "walk", res.status, res.output, res.detail)


@app.post("/api/generate")
async def generate(request: Request, background: BackgroundTasks):
    user = require_user(request)
    body = await read_json(request, max_bytes=500 * 1024)   # 500 KB cap
    url = (body.get("url") or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="a valid http(s) url is required")
    _reject_private_url(url)
    template = body.get("template") or "saas-walkthrough"
    uid = user["id"]

    # Gate 1 — sliding-window rate limit. Applies to every plan, so one account
    # (or a runaway script) can't drain the Vertex/chromium budget.
    # Atomic check-and-reserve so concurrent requests can't all slip under it.
    if not store.try_acquire(uid, "render_request",
                             limit=config.RENDER_MAX_PER_HOUR, window_seconds=3600):
        raise HTTPException(
            status_code=429,
            detail=f"rate limit: {config.RENDER_MAX_PER_HOUR} renders/hour per "
                   "account. Try again shortly.",
            headers={"Retry-After": str(
                store.window_retry_after(uid, "render_request", 3600))},
        )

    # Gate 2 — per-account concurrency. try_queue_job creates the queued job
    # atomically iff under the in-flight cap, so this IS the reservation; if
    # over, refund the rate-limit reservation and 429.
    job_id = store.try_queue_job(uid, "walk",
                                 max_concurrent=config.RENDER_MAX_CONCURRENT)
    if job_id is None:
        store.refund(uid, "render_request")
        raise HTTPException(
            status_code=429,
            detail=f"too many in-flight renders ({config.RENDER_MAX_CONCURRENT} "
                   "max). Wait for one to finish.",
            headers={"Retry-After": "60"},
        )

    name = body.get("name") or f"{template} demo"
    # scout needs headroom on top of the film render
    cap = CostCap(max_seconds=int(body.get("max_seconds", 360)))
    spec_kwargs = {
        "aspect": body.get("aspect", "16:9"),
        "brand": body.get("brand", "#ff7d1f"),
        "intro": body.get("intro", ""),
        "outro": body.get("outro", ""),
        "voice": body.get("voice", ""),
        "brief": (body.get("brief") or "")[:500],
        "cap": cap,
    }
    background.add_task(_run_generation, job_id, uid, url, template, name,
                        spec_kwargs)
    return JSONResponse({"job_id": job_id, "status": "queued"}, status_code=202)


@app.get("/api/jobs")
async def jobs_list(request: Request):
    user = require_user(request)
    jobs = store.list_jobs(user["id"])
    return {"jobs": [
        {"job_id": j["id"], "status": j["status"], "detail": j["detail"],
         "created_at": j["created_at"], "has_video": bool(j["output"])}
        for j in jobs]}


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str, request: Request):
    user = require_user(request)
    job = store.get_job(job_id)
    if not job or job["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="job not found")
    return {"job_id": job_id, "status": job["status"], "detail": job["detail"],
            "has_video": bool(job["output"])}


@app.get("/api/jobs/{job_id}/video")
async def job_video(job_id: str, request: Request):
    user = require_user(request)
    job = store.get_job(job_id)
    if not job or job["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="job not found")
    if job["status"] != "done" or not job["output"]:
        raise HTTPException(status_code=409, detail=f"job is {job['status']}")
    p = Path(job["output"]).resolve()
    # Defense in depth: keep the served video inside JOBS_DIR even if the
    # stored output path were ever tampered with (e.g. ../../../etc/passwd).
    try:
        p.relative_to(config.JOBS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid output path")
    if not p.is_file():
        raise HTTPException(status_code=410, detail="video no longer available")
    return FileResponse(str(p), media_type="video/mp4", filename=f"{job_id}.mp4")


# ---- MCP harness -------------------------------------------------------------
# Agents point an MCP client at /mcp/<token>. Auth is enforced here; the protocol is
# served by capturd-mcp.service on 127.0.0.1:8100 (its own process so uvicorn can run
# FastMCP's lifespan — mounting it in-process leaves the session manager uninitialised
# and every call 500s).
#
# /api/me used to advertise this URL while no route existed at all, so any agent that
# tried to use Captur'd as a harness got a 404.

import os
import httpx as _httpx
from fastapi import Response as _Response

_MCP_UPSTREAM = os.environ.get("CAPTURD_MCP_UPSTREAM", "http://127.0.0.1:8100")
_HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade",
               "proxy-authenticate", "proxy-authorization", "te", "trailer"}


@app.api_route("/mcp/{token}", methods=["GET", "POST", "DELETE"])
@app.api_route("/mcp/{token}/{rest:path}", methods=["GET", "POST", "DELETE"])
async def mcp_proxy(request: Request, token: str, rest: str = ""):
    user = store.user_for_mcp_token(token)
    if not user:
        return JSONResponse({"error": "invalid or revoked mcp token"}, status_code=401)

    url = f"{_MCP_UPSTREAM}/{rest}" if rest else f"{_MCP_UPSTREAM}/"
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _HOP_BY_HOP and k.lower() != "host"}
    headers["x-capturd-user"] = user["id"]
    headers["x-capturd-email"] = user["email"]
    body = await request.body()
    try:
        async with _httpx.AsyncClient(timeout=120) as cx:
            up = await cx.request(request.method, url, content=body, headers=headers,
                                  params=dict(request.query_params))
    except _httpx.RequestError as exc:
        return JSONResponse({"error": f"mcp upstream unreachable: {exc}"}, status_code=502)
    out = {k: v for k, v in up.headers.items() if k.lower() not in _HOP_BY_HOP}
    return _Response(content=up.content, status_code=up.status_code, headers=out)
