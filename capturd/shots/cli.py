"""Command-line interface for `capturd shots` — rested-state screenshot batch.

Invoked either as ``capturd shots ...`` (via the top-level dispatcher) or as
the legacy ``sunsponge-capture ...`` alias (backwards compat for older scripts).

The batch takes ONE source: explicit URLs, a sitemap, a crawl, a local build,
or a pathway map (verifier JSON or the markdown manifest) resolved against a
``--base-url``. The map path is the new wiring: a pathway map goes in and a
real capture plan comes out — ``--dry-run`` prints that plan with no browser
and no network call: a ``trigger_selector`` comes only from what the map
itself names (the ``selector``/``css`` column), and a named control that
would need the page to bind is
reported unbound (``selector=-``) with a flag. The quality gate (01c)
decides whether a MAP-DRIVEN run is evidence — a legacy ``--urls`` /
``--sitemap`` / ``--site`` / ``--local`` run keeps its pre-gate exit codes
(a baseline-only run is 0, a failed job is 1, done-with-errors is 3) — and
its reason code reaches this caller unchanged when a gate runs. Finished map
runs refused by quality exit 4. Only CLI map inputs permit local map files and
loopback navigation on the exact --base-url hostname. Legacy URL/sitemap/crawl/local
capture permits loopback only on each target's exact host. Explicitly allowed private
hosts are a separate exact-host opt-in; metadata always remains refused. Sitemap/crawl
discovery remains loopback-refused. Remote callers
pass map_text/manifest_text and default to no loopback permission.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
import time
from dataclasses import asdict

from .capture import (
    RestedCaptureError,
    RestedCaptureManager,
    _redact_url_userinfo,
    _safe_settings,
    build_capture_plan,
    build_quality_summary,
)
from .pathway_plan import MAX_TARGETS
from .pathway_map import PathwayMapError
from .quality import build_quality_error, is_passing, summarize_capture_quality

#: Exit code for a finished map-driven run the quality gate refuses to call
#: evidence. The gate is map-only (D93) — a legacy run never gets this code.
EXIT_QUALITY_FAIL = 4


def _split(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").replace("\n", ",").split(",") if item.strip()]


def _normalize_private_hosts(values: list[str]) -> tuple[str, ...]:
    message = "allow-private-host must be a bare host or bracketed IPv6 literal"
    hosts: list[str] = []
    for value in values:
        host = value.strip().lower()
        ipv6_literal = False
        if host.startswith("[") and host.endswith("]"):
            literal = host[1:-1]
            try:
                if ipaddress.ip_address(literal).version != 6:
                    raise ValueError
            except ValueError:
                raise RestedCaptureError(message) from None
            host = literal
            ipv6_literal = True
        elif any(char in host for char in "[]"):
            raise RestedCaptureError(message)
        if (
            not host
            or any(char.isspace() for char in host)
            or any(char in host for char in ("/?#@*" if ipv6_literal else ":/?#@*"))
        ):
            raise RestedCaptureError(message)
        host = host.rstrip(".")
        if not host:
            raise RestedCaptureError(message)
        if host not in hosts:
            hosts.append(host)
    return tuple(hosts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capturd shots",
        description=(
            "Rested-state website screenshot batch - settled, animation-free, "
            "full-page shots across viewports and color schemes. "
            "(Also invokable as `sunsponge-capture` for backwards compat.)"
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--site", metavar="URL", help="Crawl a site and capture every discovered page")
    source.add_argument("--sitemap", metavar="URL", help="Capture every page listed in a sitemap.xml")
    source.add_argument("--local", metavar="PATH", help="Capture a local .html file, or a folder of them")
    source.add_argument("--urls", metavar="A,B,C", help="Comma- or newline-separated list of URLs")
    source.add_argument("--pathway-map", metavar="JSON",
                        help="Pathway map file in verifier JSON (checks with findings) - not the markdown manifest (see --pathway-manifest)")
    source.add_argument("--pathway-manifest", metavar="MD",
                        help="Pathway manifest file in markdown (headings and pipe tables) - not verifier JSON (see --pathway-map)")
    source.add_argument("--map-text", metavar="TEXT",
                        help="pasted verifier JSON map text - the same content as --pathway-map, no file on disk")

    parser.add_argument("--base-url", metavar="URL",
                        help="Base URL the map's pathways resolve against - required with a map input; CLI permits loopback only on this exact host, never metadata")
    parser.add_argument("--allow-private-host", metavar="HOST", action="append", default=[],
                        help="Repeatable exact-host RFC1918/ULA/loopback opt-in; never permits metadata")
    parser.add_argument("--max-targets", type=int, default=MAX_TARGETS, metavar="N",
                        help="Refuse a map naming more than N capture targets (default %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the capture plan and exit - no shots. A map plan "
                             "launches no browser and makes no network call: trigger_selector "
                             "comes only from the selector/css column, and a named control that "
                             "would need the page to bind is reported unbound (selector=-) with "
                             "a flag. --site / --sitemap combinations are refused because "
                             "their discovery requires network access.")

    parser.add_argument("--viewports", default="desktop,tablet,mobile",
                        help="Any of desktop,tablet,mobile (default: all three)")
    parser.add_argument("--schemes", default="light,dark",
                        help="Any of light,dark (default: both)")
    parser.add_argument("--format", default="png", choices=["png", "jpeg"], help="Image format")
    parser.add_argument("--no-full-page", action="store_true",
                        help="Capture only the viewport instead of the full scrollable page")
    parser.add_argument("--out", metavar="DIR", help="Export folder (default: a ZIP in the app data dir)")
    parser.add_argument("--zip", action="store_true", help="Force a ZIP export even when --out is given")
    parser.add_argument("--depth", type=int, default=8, help="Crawl depth for --site (default: 8)")
    parser.add_argument("--max-pages", type=int, default=1000, help="Max pages to discover (default: 1000)")
    parser.add_argument("--workers", type=int, default=3, help="Concurrent capture workers (default: 3)")
    parser.add_argument("--cookie", metavar="NAME=VALUE;DOMAIN", action="append", default=[],
                        help="Send a cookie so signed-in pages can be captured, e.g. "
                             "--cookie 'session=abc123;app.example.com'. Repeatable. Without this "
                             "the batch can only shoot pages a logged-out visitor can reach.")
    parser.add_argument("--states", action="store_true",
                        help="Also capture every state a page can be put into: click each "
                             "interactive control (modals, dropdowns, tabs, accordions, "
                             "toggles), shoot what it opens, then restore. No LLM involved.")
    parser.add_argument("--states-limit", type=int, default=20, metavar="N",
                        help="Max controls to sweep per page (default: 20)")
    parser.add_argument("--state-wait-ms", type=int, default=900, metavar="MS",
                        help="Wait after each click before shooting (default: 900)")
    parser.add_argument("--settle-ms", type=int, default=600,
                        help="Settle wait per page, in ms, before the shot (default: 600)")
    return parser


def _parse_cookies(raw: list[str]) -> list[dict]:
    """`name=value;domain` -> the cookie dicts Playwright wants.

    Without this the batch can only ever capture what a logged-out visitor sees, which
    leaves every authenticated product — the actual app — impossible to screenshot.
    """
    out = []
    for item in raw or []:
        pair, _, domain = item.partition(";")
        name, _, value = pair.partition("=")
        name, value, domain = name.strip(), value.strip(), domain.strip()
        if not (name and value and domain):
            raise SystemExit(f"error: --cookie must look like NAME=VALUE;DOMAIN (got {item!r})")
        # A `secure` cookie is never sent over http, so marking one for localhost would
        # silently drop it and every local capture would come back logged out.
        local = domain.split(":")[0] in ("localhost", "127.0.0.1", "::1", "0.0.0.0")
        out.append({"name": name, "value": value, "domain": domain,
                    "path": "/", "secure": not local, "httpOnly": False})
    return out


def _print_plan(urls, targets, settings) -> int:
    """The capture plan, human lines plus one deterministic machine line.

    The ``plan-json:`` line is what a caller can diff across runs — same input,
    same bytes. Nothing here or there carries a host path.
    """
    safe_urls = [_redact_url_userinfo(url) for url in urls]
    print(f"capture plan: {len(safe_urls)} page(s), {len(targets)} target(s)")
    for url in safe_urls:
        print(f"url: {url}")
    for target in targets:
        print(
            f"target {target.index:03d} pathway={target.pathway_id or '-'} "
            f"status={target.pathway_status or '-'} selector={target.trigger_selector or '-'} "
            f"state={target.state_id} url={_redact_url_userinfo(target.url)}"
        )
    for flag in settings.get("pathway_flags") or []:
        print(f"flag: {flag['flag']} {flag['pathway_id']}: {flag['reason']}")
    # Cookie values are credentials; keep them out of shareable dry-run output.
    safe_settings = _safe_settings(settings)
    print(
        "plan-json: "
        + json.dumps(
            {
                "urls": safe_urls,
                "targets": [
                    {**asdict(target), "url": _redact_url_userinfo(target.url)}
                    for target in targets
                ],
                "settings": safe_settings,
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.dry_run and (args.site or args.sitemap):
        print(
            "error: --dry-run does not support --site or --sitemap because discovery requires network access",
            file=sys.stderr,
        )
        return 2

    map_input = bool(args.pathway_map or args.pathway_manifest or args.map_text)
    if map_input and not (args.base_url or "").strip():
        print("error: base_url is required with a pathway map - point Captur'd at the built HTML with --base-url", file=sys.stderr)
        return 2
    try:
        allow_private_hosts = _normalize_private_hosts(args.allow_private_host)
    except RestedCaptureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = {
        "urls": _split(args.urls) if args.urls else [],
        "sitemap_url": args.sitemap or "",
        "crawl": bool(args.site),
        "crawl_url": args.site or "",
        "local": bool(args.local),
        "local_path": args.local or "",
        "crawl_depth": args.depth,
        "max_pages": args.max_pages,
        "viewports": _split(args.viewports),
        "schemes": _split(args.schemes),
        "format": args.format,
        "full_page": not args.no_full_page,
        "export_mode": "folder" if (args.out and not args.zip) else "zip",
        "export_dir": args.out or None,
        "concurrency": args.workers,
        "wait_ms": args.settle_ms,
        "cookies": _parse_cookies(args.cookie),
        "states": args.states,
        "states_limit": args.states_limit,
        "state_wait_ms": args.state_wait_ms,
        # D92: dry-run planning navigates nothing (the map planner binds no
        # selectors from the page).
        "dry_run": bool(args.dry_run),
        "name": "sunsponge-captures",
    }
    if map_input:
        payload.update({
            "map_path": args.pathway_map,
            "manifest_path": args.pathway_manifest,
            "map_text": args.map_text,
            "base_url": args.base_url,
            "max_targets": args.max_targets,
        })

    capabilities = {
        "allow_map_files": map_input,
        "allow_loopback": True,
        "allow_private_hosts": allow_private_hosts,
    }
    if args.dry_run:
        try:
            urls, targets, settings = build_capture_plan(payload, **capabilities)
        except (RestedCaptureError, PathwayMapError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return _print_plan(urls, targets, settings)

    manager = RestedCaptureManager()
    try:
        job = manager.start(payload, **capabilities)
    except (RestedCaptureError, PathwayMapError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    job_id = job["job_id"]
    last = -1
    while True:
        job = manager.get(job_id)
        done, total = int(job.get("completed", 0)), int(job.get("total", 0))
        if done != last:
            print(f"\r{job.get('message', '')}  [{done}/{total}]", end="", flush=True)
            last = done
        if job.get("status") in {"done", "done_with_errors", "failed"}:
            break
        time.sleep(0.5)
    print()

    status = job.get("status")
    if status == "failed":
        print(f"failed: {job.get('message')}", file=sys.stderr)
        return 1

    total = int(job.get("total", 0))
    failed = int(job.get("failed", 0))
    print(f"done: {total - failed}/{total} captured" + (f"  ({failed} failed)" if failed else ""))
    if job.get("zip_path"):
        print(f"zip:  {job['zip_path']}")
    if job.get("output_dir") and job.get("output_dir") != job.get("zip_path"):
        print(f"out:  {job['output_dir']}")

    # D93: the quality gate (01c) decides only for MAP-DRIVEN runs — a
    # --urls / --sitemap / --site / --local run exits exactly as it did
    # before this PR's gate (a baseline-only run is 0, a failed job keeps its
    # old code, done-with-errors is 3). When a gate runs its reason code
    # reaches the caller unchanged - no re-wording downstream, honest-failure
    # at the exit code.
    if status != "done":
        return 3
    if map_input:
        summary = build_quality_summary(job.get("results") or [])
        quality = summarize_capture_quality(summary)
        if not is_passing(summary):
            print(f"quality: {' '.join(quality.reason_codes)}", file=sys.stderr)
            detail = build_quality_error(quality)
            if detail:
                print(detail, file=sys.stderr)
            return EXIT_QUALITY_FAIL
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
