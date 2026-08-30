"""Run the Captur'd MCP harness as its own HTTP service.

Mounting FastMCP's streamable-HTTP app inside the existing FastAPI app requires its
lifespan to run in the parent's constructor; bolting it on afterwards leaves the session
manager's task group uninitialised and every call 500s. Running it as its own ASGI app
lets uvicorn manage the lifespan properly, and the main service just proxies to it.

Listens on 127.0.0.1:8100 — never exposed directly; auth happens in the Captur'd service.

V2: the core surface (capture.*/demo.*/voice.*) is augmented with the HOSTED
engagement tools (demo.publish, demo.version.*, analytics.*, share.*) when the
service database is present. The MCP proxy injects x-capturd-user; the ASGI
middleware below captures it so engagement tools can scope ownership.
"""
import sys, os

_here = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.dirname(_here)          # repo root (local AND /opt deploy)
sys.path.insert(0, _here)
sys.path.insert(0, _repo_root)

from capturd.mcp.server import _build_server  # noqa: E402

server = _build_server()

# ---- hosted engagement tools (V2) -------------------------------------------
# They register only when the service app (store/config) is importable; a bare
# local run of this file stays a pure core+voice surface.
try:
    from app import analytics, config, frontman, store  # noqa: E402
    import engagement_tools  # noqa: E402
except Exception:  # noqa: BLE001 — engagement layer is optional at import time
    analytics = config = frontman = store = engagement_tools = None

if engagement_tools is not None:
    config.ensure_dirs()
    store.init()
    server.mount(engagement_tools.build_server(store, analytics, frontman))

    class _UserContextMiddleware:
        """Capture x-capturd-user (injected by the service's MCP proxy) into the
        contextvar the engagement tools read for ownership checks."""

        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                           for k, v in scope.get("headers", [])}
                uid = headers.get("x-capturd-user", "")
                if uid:
                    engagement_tools.user_id_var.set(uid)
            await self.app(scope, receive, send)

    app = server.http_app(path="/")
    app.add_middleware(_UserContextMiddleware)
else:
    app = server.http_app(path="/")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("CAPTURD_MCP_PORT", "8100")))
