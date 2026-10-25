"""Shared constants for the live-drive E2E harness — small and dependency-free.

Deliberately a separate module so tests can import ``SERVER_FIXTURE_WRAPPER``
WITHOUT importing the whole ``e2e_live_drive`` driver, which pulls in the MCP
stdio client and, transitively, playwright.

``SERVER_FIXTURE_WRAPPER`` is the source of the explicit TEST-ONLY wrapper the
live-drive harness (``scripts/e2e_live_drive.py``) uses to launch the MCP
server subprocess. It registers the harness's loopback fixture origin with the
canonical seam in ``capturd._net`` (which refuses anything that is not a
loopback IP literal with an explicit port) and wraps the server's URL guard so
exactly that one origin is allowed. Every other URL — metadata, RFC1918, other
loopback ports — still hits the real fail-closed guard. Production never runs
this wrapper; production processes (``python -m capturd.mcp.server``, the
hosted service) never register anything and stay fail-closed.

This module deliberately ships NO licence provisioning: the Pro gate reads its
codes/licence from the environment (``capturd/pro.py``), so anything this
harness set would be a working licence bypass checked into the repo. The
driver takes the licence from the operator's own environment and skips the
demo.* leg loudly when there is none. (Issue #44 tracks the gate design
itself; this PR only removes the provisioning from the tree.)
"""

SERVER_FIXTURE_WRAPPER = """\
import sys

from capturd import _net
import capturd.mcp.server as _srv

_net.register_test_fixture_origin(sys.argv[1])
_srv._reject_private_url = _net.guard_with_test_fixture_allowance(_srv._reject_private_url)

from capturd.mcp.server import main  # noqa: E402

sys.exit(main())
"""
