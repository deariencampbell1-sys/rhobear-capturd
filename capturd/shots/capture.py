"""Website rested-state capture jobs.

This module is intentionally usable without Playwright installed: imports stay
clean, and dependency failures are reported on the job instead of crashing the
API process.
"""

from __future__ import annotations

import asyncio
import gzip
import ipaddress
import json
import os
import re
import shutil
import socket
import threading
import time
import uuid
import zipfile
import warnings
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import request as urlrequest
from urllib.request import url2pathname
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from xml.etree import ElementTree

from capturd._net import is_private_ip
from capturd.shots.pathway_map import PathwayMapError, load_pathway_map


DEFAULT_VIEWPORTS: dict[str, dict[str, int]] = {
    "desktop": {"width": 1440, "height": 1000},
    "tablet": {"width": 834, "height": 1112},
    "mobile": {"width": 390, "height": 844},
}
DEFAULT_SCHEMES = ("light", "dark")
MAX_URLS = 1000
MAX_SITEMAP_URLS = 1000
MAX_CRAWL_URLS = 1000
DEFAULT_TIMEOUT_MS = 30000
TRIGGER_CLICK_TIMEOUT_MS = 2500
TRIGGER_STATE_TIMEOUT_MS = 2500
NAV_GUARD_TIMEOUT_S = 10
DEFAULT_WAIT_MS = 600
DEFAULT_CONCURRENCY = 3
DEFAULT_RETRIES = 1
DEFAULT_RETRY_TIMEOUT_MS = 60000
DEFAULT_CRAWL_DEPTH = 8
DEFAULT_CRAWL_CONCURRENCY = 6
DEFAULT_DISCOVERY_TIMEOUT_MS = 15000
DEFAULT_DISCOVERY_WAIT_MS = 150

USER_AGENT = "Capturd/0.2"

NON_PAGE_EXTENSIONS = {
    ".7z",
    ".avi",
    ".avif",
    ".bmp",
    ".css",
    ".csv",
    ".doc",
    ".docx",
    ".eot",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".map",
    ".mjs",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".otf",
    ".pdf",
    ".png",
    ".rar",
    ".rss",
    ".svg",
    ".tar",
    ".tgz",
    ".tif",
    ".tiff",
    ".ts",
    ".ttf",
    ".txt",
    ".wasm",
    ".wav",
    ".webm",
    ".webmanifest",
    ".webp",
    ".woff",
    ".woff2",
    ".xml",
    ".zip",
}

LOCAL_PAGE_EXTENSIONS = {".html", ".htm"}
LOCAL_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    "target",
}
LOCAL_SKIP_FILE_PREFIXES = ("_", ".")

TRACKING_QUERY_KEYS = {"fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "msclkid"}

REST_CSS = """
*, *::before, *::after {
  animation-delay: -1ms !important;
  animation-duration: 1ms !important;
  animation-iteration-count: 1 !important;
  scroll-behavior: auto !important;
  transition-delay: 0s !important;
  transition-duration: 0s !important;
  caret-color: transparent !important;
}
html { scroll-behavior: auto !important; }
""".strip()


class RestedCaptureError(ValueError):
    """User-correctable capture request error."""


@dataclass(frozen=True)
class CaptureTarget:
    index: int
    url: str
    viewport_id: str
    width: int
    height: int
    scheme: str
    # Map-driven plans (capturd.shots.pathway_plan) carry the pathway identity
    # and the named control's planner selector; URL-driven plans leave them
    # empty. Nothing downstream branches on which input arrived.
    pathway_id: str = ""
    pathway_status: str = ""
    pathway_trigger: str = ""
    trigger_selector: str = ""  # planner selector to click after load, to reach the rested state

    @property
    def state_id(self) -> str:
        if self.pathway_id:
            status = _slug(self.pathway_status or "unknown", "unknown")
            pid = _slug(self.pathway_id, "pathway")
            return f"{pid}-{status}-{self.viewport_id}-{self.scheme}"
        return f"{self.viewport_id}-{self.scheme}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _app_data_dir() -> Path:
    configured = os.environ.get("SUNSPONGE_CAPTURE_DATA")
    if configured:
        return Path(configured).expanduser()
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if local:
        return Path(local) / "SunSpongeCapture"
    return Path.home() / ".sunsponge-capture"


def _slug(value: str, fallback: str = "site") -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text[:80] or fallback


def _job_name() -> str:
    return "rested-captures-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _is_local_input(value: str) -> bool:
    raw = (value or "").strip().strip('"')
    if not raw:
        return False
    if raw.lower().startswith("file://"):
        return True
    if re.match(r"^[a-zA-Z]:[\\/]", raw) or raw.startswith("\\\\"):
        return True
    return Path(raw).expanduser().exists()


def _file_url_from_path(path: Path) -> str:
    return path.expanduser().resolve().as_uri()


def _path_from_file_url(value: str) -> Path:
    parsed = urlparse(value)
    if parsed.scheme != "file":
        raise RestedCaptureError(f"unsupported local URL: {value}")
    raw_path = url2pathname(parsed.path)
    if os.name == "nt" and re.match(r"^/[a-zA-Z]:/", raw_path):
        raw_path = raw_path[1:]
    return Path(raw_path)


def _local_path_from_value(value: str) -> Path:
    raw = (value or "").strip().strip('"')
    if not raw:
        raise RestedCaptureError("empty local path")
    path = _path_from_file_url(raw) if raw.lower().startswith("file://") else Path(raw).expanduser()
    if not path.exists():
        raise RestedCaptureError(f"local path does not exist: {value}")
    return path.resolve()


def _normalize_local_url(value: str) -> str:
    path = _local_path_from_value(value)
    if path.is_dir():
        index = path / "index.html"
        if index.is_file():
            path = index
        else:
            raise RestedCaptureError(f"local folder has no index.html: {value}")
    if path.suffix.lower() not in LOCAL_PAGE_EXTENSIONS:
        raise RestedCaptureError(f"local file is not HTML: {value}")
    return _file_url_from_path(path)


def _local_key(value: str) -> str:
    try:
        path = _path_from_file_url(value)
    except RestedCaptureError:
        path = Path(value)
    key = str(path.expanduser().resolve())
    return key.lower() if os.name == "nt" else key


def _clean_query(query: str) -> str:
    pairs = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        lower = key.lower()
        if lower.startswith("utm_") or lower in TRACKING_QUERY_KEYS:
            continue
        pairs.append((key, value))
    return urlencode(pairs, doseq=True)


def _site_host_key(host: str | None) -> str:
    value = (host or "").strip().lower().rstrip(".")
    if value.startswith("www."):
        value = value[4:]
    return value


def _canonical_page_url(value: str, base: str | None = None, preferred_netloc: str | None = None) -> str:
    raw = (value or "").strip()
    if not raw:
        raise RestedCaptureError("empty URL")
    if raw.startswith(("#", "mailto:", "tel:", "sms:", "javascript:", "data:", "blob:")):
        raise RestedCaptureError(f"unsupported URL: {value}")
    if _is_local_input(raw):
        return _normalize_local_url(raw)
    if base:
        raw = urljoin(base, raw)
    if _is_local_input(raw):
        return _normalize_local_url(raw)
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RestedCaptureError(f"unsupported URL: {value}")

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    if preferred_netloc:
        preferred = urlparse(f"https://{preferred_netloc}").netloc.lower()
        if _site_host_key(parsed.hostname) == _site_host_key(urlparse(f"https://{preferred}").hostname):
            netloc = preferred

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    path = re.sub(r"/(?:index|default)\.html?$", "/", path, flags=re.IGNORECASE)
    query = _clean_query(parsed.query)
    return urlunparse((scheme, netloc, path, "", query, ""))


def _url_key(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme == "file":
        return "file://" + _local_key(value)
    host = _site_host_key(parsed.hostname)
    port = f":{parsed.port}" if parsed.port else ""
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    path = path.rstrip("/") if path != "/" else "/"
    query = _clean_query(parsed.query)
    return urlunparse((parsed.scheme.lower(), host + port, path, "", query, ""))


def _looks_like_page_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme == "file":
        return _path_from_file_url(value).suffix.lower() in LOCAL_PAGE_EXTENSIONS
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    suffix = Path(parsed.path.lower()).suffix
    return suffix not in NON_PAGE_EXTENSIONS


def _is_same_site(value: str, allowed_site_keys: set[str]) -> bool:
    return _site_host_key(urlparse(value).hostname) in allowed_site_keys


def normalize_url(value: str) -> str:
    return _canonical_page_url(value)


def _dedupe_urls(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        url = normalize_url(value)
        key = _url_key(url)
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
    return out


def _parse_sitemap_xml(xml_bytes: bytes) -> tuple[list[str], list[str]]:
    root = ElementTree.fromstring(xml_bytes)
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}", 1)[0] + "}"

    page_urls: list[str] = []
    sitemap_urls: list[str] = []
    if root.tag.endswith("urlset"):
        for loc in root.findall(f".//{ns}url/{ns}loc"):
            if loc.text and loc.text.strip():
                page_urls.append(loc.text.strip())
    elif root.tag.endswith("sitemapindex"):
        for loc in root.findall(f".//{ns}sitemap/{ns}loc"):
            if loc.text and loc.text.strip():
                sitemap_urls.append(loc.text.strip())
    return page_urls, sitemap_urls


def _unwrapped_address(ip: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address behind *ip* with any IPv4-mapped wrapper removed.

    ``::ffff:127.0.0.1`` is loopback in IPv4 clothing — classify the unwrapped
    address so the one loopback exception below cannot be bypassed (or, for
    anything else, widened) through a mapped form. Unparseable input yields
    ``None`` and is never treated as loopback (fail closed).
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    mapped = getattr(addr, "ipv4_mapped", None)
    return mapped if mapped is not None else addr


def _reject_private_url(
    url: str, *, allow_loopback: bool = False, allow_private_peer: bool = False,
) -> None:
    """Refuse private/internal peers, with narrow explicit local exceptions."""
    host = urlparse(url).hostname
    if not host:
        raise RestedCaptureError("could not parse host from url")
    try:
        addrinfo = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise RestedCaptureError(f"hostname not found: {exc}") from exc
    for family, _type, _proto, _canon, sockaddr in addrinfo:
        ip = sockaddr[0]
        if is_private_ip(ip):
            unwrapped = _unwrapped_address(ip)
            if unwrapped is not None:
                private_lan = (
                    unwrapped.is_loopback
                    or (
                        unwrapped.version == 4
                        and any(
                            unwrapped in ipaddress.ip_network(network)
                            for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
                        )
                    )
                    or (
                        unwrapped.version == 6
                        and unwrapped in ipaddress.ip_network("fc00::/7")
                    )
                )
                if (
                    (allow_loopback and unwrapped.is_loopback)
                    or (allow_private_peer and private_lan)
                ):
                    continue
                if (
                    unwrapped.is_link_local
                    or unwrapped.is_reserved
                    or unwrapped.is_multicast
                    or unwrapped.is_unspecified
                ):
                    raise RestedCaptureError(
                        f"url resolves to a private/internal IP ({ip}) — not allowed"
                    )
            raise RestedCaptureError(f"url resolves to a private/internal IP ({ip}) — not allowed")


def _canonical_host(host: str | None) -> str:
    return (host or "").strip().lower().rstrip(".")


def _reject_navigation_url(
    url: str, *, allow_loopback: bool, loopback_host: str | None,
    allow_private_hosts: tuple[str, ...] = (),
) -> None:
    # Preserve local-file capture; only HTTP(S) requests have DNS/IP policy.
    if urlparse(url).scheme.lower() not in {"http", "https"}:
        return
    host = _canonical_host(urlparse(url).hostname)
    permitted = bool(
        allow_loopback and loopback_host and host == _canonical_host(loopback_host)
    )
    private_permitted = host in {
        _canonical_host(allowed) for allowed in allow_private_hosts
    }
    private_kwargs = {"allow_private_peer": True} if private_permitted else {}
    _reject_private_url(url, allow_loopback=permitted, **private_kwargs)


async def _check_navigation_url(
    url: str, *, allow_loopback: bool, loopback_host: str | None,
    allow_private_hosts: tuple[str, ...] = (),
    cache: dict[tuple[Any, ...], RestedCaptureError | None] | None = None,
) -> None:
    parsed = urlparse(url)
    key = (
        parsed.scheme.lower(), _canonical_host(parsed.hostname), allow_loopback,
        _canonical_host(loopback_host),
        tuple(_canonical_host(host) for host in allow_private_hosts),
    )
    if cache is not None and key in cache:
        cached = cache[key]
        if cached is not None:
            raise cached
        return
    resolver_kwargs = {
        "allow_loopback": allow_loopback, "loopback_host": loopback_host,
    }
    if allow_private_hosts:
        resolver_kwargs["allow_private_hosts"] = allow_private_hosts
    try:
        await asyncio.wait_for(asyncio.to_thread(
            _reject_navigation_url, url, **resolver_kwargs,
        ), timeout=NAV_GUARD_TIMEOUT_S)
    except RestedCaptureError as exc:
        if cache is not None:
            cache[key] = exc
        raise
    except (asyncio.TimeoutError, TimeoutError):
        raise RestedCaptureError("navigation URL check timed out") from None
    if cache is not None:
        cache[key] = None


async def _install_navigation_guard(
    context: Any, *, allow_loopback: bool, loopback_host: str | None,
    allow_private_hosts: tuple[str, ...] = (),
    navigation_cache: dict[tuple[Any, ...], RestedCaptureError | None] | None = None,
    guard_errors: list[RestedCaptureError] | None = None,
) -> list[RestedCaptureError]:
    """Check requests before Chromium connects; retain the first refusal.

    Chromium resolves again at connect time. This userspace guard cannot pin
    its socket DNS; rebinding between check and connect remains a residual.
    Coroutine deadlines cannot terminate an OS resolver already in a thread.
    """
    if guard_errors is None:
        guard_errors = []

    async def guard(route: Any) -> None:
        try:
            await _check_navigation_url(
                route.request.url, allow_loopback=allow_loopback,
                loopback_host=loopback_host, allow_private_hosts=allow_private_hosts,
                cache=navigation_cache,
            )
        except Exception as exc:
            original_class = type(exc).__name__
            if not isinstance(exc, RestedCaptureError):
                exc = RestedCaptureError(f"navigation URL check failed ({original_class})")
            await route.abort()
            resource_type = getattr(route.request, "resource_type", "document")
            if resource_type == "document":
                if not guard_errors:
                    guard_errors.append(exc)
                return
            warnings.warn(
                f"capture subresource refused ({original_class}); request aborted",
                RuntimeWarning,
                stacklevel=2,
            )
            return
        await route.continue_()

    await context.route("**/*", guard)
    return guard_errors


def _fetch_url_bytes(url: str, timeout: int = 20, limit: int = 10 * 1024 * 1024) -> tuple[bytes, dict[str, str]]:
    _reject_private_url(url)
    req = urlrequest.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        body = resp.read(limit)
        headers = {key.lower(): value for key, value in resp.headers.items()}
    if headers.get("content-encoding", "").lower() == "gzip" or urlparse(url).path.endswith(".gz"):
        body = gzip.decompress(body)
    return body, headers


def expand_sitemap(sitemap_url: str, limit: int = MAX_SITEMAP_URLS) -> list[str]:
    root_url = normalize_url(sitemap_url)
    pending = [root_url]
    seen_sitemaps: set[str] = set()
    out: list[str] = []

    while pending and len(out) < limit:
        current = pending.pop(0)
        if current in seen_sitemaps:
            continue
        seen_sitemaps.add(current)

        body, _headers = _fetch_url_bytes(current, timeout=20)
        page_urls, child_sitemaps = _parse_sitemap_xml(body)
        for url in page_urls:
            if len(out) >= limit:
                break
            out.append(url)
        for child in child_sitemaps:
            if child not in seen_sitemaps and len(pending) < 50:
                pending.append(child)

    return _dedupe_urls(out)


def _sitemap_hints_for_seed(seed_url: str) -> list[str]:
    parsed = urlparse(normalize_url(seed_url))
    origin = f"{parsed.scheme}://{parsed.netloc}"
    hints: list[str] = []
    try:
        body, _headers = _fetch_url_bytes(f"{origin}/robots.txt", timeout=8, limit=1024 * 1024)
        for line in body.decode("utf-8", errors="ignore").splitlines():
            if line.lower().startswith("sitemap:"):
                sitemap = line.split(":", 1)[1].strip()
                if sitemap:
                    hints.append(sitemap)
    except Exception:
        pass
    hints.extend([
        f"{origin}/sitemap.xml",
        f"{origin}/sitemap_index.xml",
        f"{origin}/sitemap-index.xml",
    ])
    return _dedupe_urls(hints)


def same_site_page_urls(base_url: str, raw_links: list[str], allowed_site_keys: set[str]) -> list[str]:
    base = normalize_url(base_url)
    preferred_netloc = urlparse(base).netloc
    urls: list[str] = []
    for raw in raw_links:
        try:
            candidate = _canonical_page_url(raw, base=base, preferred_netloc=preferred_netloc)
        except RestedCaptureError:
            continue
        if not _looks_like_page_url(candidate) or not _is_same_site(candidate, allowed_site_keys):
            continue
        urls.append(candidate)
    return _dedupe_urls(urls)


def _extract_html_links(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    links = re.findall(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", text, flags=re.IGNORECASE)
    links.extend(re.findall(r"""(?:href|src)\s*=\s*([^\s"'<>]+)""", text, flags=re.IGNORECASE))
    return [link.strip() for link in links if link.strip()]


def _iter_local_html(root: Path, limit: int) -> list[Path]:
    html_files: list[Path] = []
    for path in root.rglob("*"):
        if len(html_files) >= limit:
            break
        if any(part in LOCAL_SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if (
            path.is_file()
            and path.suffix.lower() in LOCAL_PAGE_EXTENSIONS
            and not path.name.startswith(LOCAL_SKIP_FILE_PREFIXES)
        ):
            html_files.append(path.resolve())

    def sort_key(path: Path) -> tuple[int, str]:
        name = path.name.lower()
        priority = {
            "index.html": 0,
            "home.html": 1,
        }.get(name, 10)
        return priority, str(path).lower()

    return sorted(html_files, key=sort_key)


def discover_local_html(
    local_value: str,
    *,
    max_urls: int = MAX_CRAWL_URLS,
    max_depth: int = DEFAULT_CRAWL_DEPTH,
) -> tuple[list[str], dict[str, Any]]:
    entry = _local_path_from_value(local_value)
    max_urls = max(1, min(MAX_CRAWL_URLS, int(max_urls or MAX_CRAWL_URLS)))
    max_depth = max(0, min(20, int(max_depth or DEFAULT_CRAWL_DEPTH)))

    if entry.is_dir():
        root = entry
        files = _iter_local_html(root, max_urls)
        if not files:
            raise RestedCaptureError(f"local folder has no HTML files: {local_value}")
        urls = [_file_url_from_path(path) for path in files[:max_urls]]
        return urls, {
            "mode": "local",
            "root": str(root),
            "seed_urls": [_file_url_from_path(files[0])],
            "page_count": len(urls),
            "max_urls": max_urls,
            "max_depth": 0,
            "crawl_log": [
                {
                    "url": _file_url_from_path(path),
                    "final_url": _file_url_from_path(path),
                    "depth": 0,
                    "ok": True,
                    "status": "local",
                    "link_count": len(_extract_html_links(path)),
                    "elapsed_ms": 0,
                    "error": None,
                }
                for path in files[:max_urls]
            ],
        }

    if entry.suffix.lower() not in LOCAL_PAGE_EXTENSIONS:
        raise RestedCaptureError(f"local file is not HTML: {local_value}")

    root = entry.parent
    pending: list[tuple[Path, int]] = [(entry.resolve(), 0)]
    queued = {_local_key(_file_url_from_path(entry))}
    seen: set[str] = set()
    out: list[Path] = []
    crawl_log: list[dict[str, Any]] = []
    while pending and len(out) < max_urls:
        current, depth = pending.pop(0)
        key = _local_key(_file_url_from_path(current))
        if key in seen or depth > max_depth:
            continue
        seen.add(key)
        out.append(current)
        links = _extract_html_links(current)
        crawl_log.append({
            "url": _file_url_from_path(current),
            "final_url": _file_url_from_path(current),
            "depth": depth,
            "ok": True,
            "status": "local",
            "link_count": len(links),
            "elapsed_ms": 0,
            "error": None,
        })
        if depth >= max_depth:
            continue
        base = _file_url_from_path(current)
        for link in links:
            try:
                candidate = _canonical_page_url(link, base=base)
                candidate_path = _path_from_file_url(candidate).resolve()
            except RestedCaptureError:
                continue
            try:
                candidate_path.relative_to(root)
            except ValueError:
                continue
            if candidate_path.suffix.lower() not in LOCAL_PAGE_EXTENSIONS:
                continue
            candidate_key = _local_key(_file_url_from_path(candidate_path))
            if candidate_key in queued or candidate_key in seen:
                continue
            queued.add(candidate_key)
            pending.append((candidate_path, depth + 1))

    urls = [_file_url_from_path(path) for path in out]
    return urls, {
        "mode": "local",
        "root": str(root),
        "seed_urls": [_file_url_from_path(entry)],
        "page_count": len(urls),
        "max_urls": max_urls,
        "max_depth": max_depth,
        "crawl_log": crawl_log,
    }


async def _extract_links(page: Any) -> list[str]:
    try:
        links = await page.evaluate(
            """() => {
              const out = [];
              const push = (value) => {
                if (typeof value === "string" && value.trim()) out.push(value);
              };
              for (const el of Array.from(document.querySelectorAll("a[href], area[href]"))) {
                push(el.href || el.getAttribute("href"));
              }
              for (const el of Array.from(document.querySelectorAll("link[href]"))) {
                const rel = (el.getAttribute("rel") || "").toLowerCase();
                if (/(canonical|alternate|next|prev)/.test(rel)) push(el.href || el.getAttribute("href"));
              }
              for (const el of Array.from(document.querySelectorAll("[data-href], [data-url]"))) {
                push(el.getAttribute("data-href"));
                push(el.getAttribute("data-url"));
              }
              return out;
            }"""
        )
    except Exception:
        return []
    return [str(item) for item in links if str(item).strip()]


async def _block_discovery_assets(route: Any) -> None:
    try:
        resource_type = route.request.resource_type
        if resource_type in {"font", "image", "media"}:
            await route.abort()
            return
    except Exception:
        pass
    await route.continue_()


async def _crawl_one_page(context: Any, url: str, timeout_ms: int, wait_ms: int) -> dict[str, Any]:
    page = None
    started = time.perf_counter()
    try:
        page = await context.new_page()
        page.set_default_timeout(timeout_ms)
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 3000))
        except Exception:
            pass
        if wait_ms > 0:
            await page.wait_for_timeout(wait_ms)
        final_url = normalize_url(page.url)
        links = await _extract_links(page)
        return {
            "ok": True,
            "url": url,
            "final_url": final_url,
            "status": response.status if response is not None else None,
            "links": links,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        return {
            "ok": False,
            "url": url,
            "final_url": url,
            "links": [],
            "error": str(exc),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass


async def _discover_site_urls_async(
    seed_urls: list[str],
    allowed_site_keys: set[str],
    max_urls: int,
    max_depth: int,
    concurrency: int,
    timeout_ms: int,
    wait_ms: int,
) -> tuple[list[str], list[dict[str, Any]]]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RestedCaptureError(
            "Playwright is required for site discovery. Install engine requirements first."
        ) from exc

    pending: list[tuple[str, int]] = [(url, 0) for url in _dedupe_urls(seed_urls)]
    queued_keys = {_url_key(url) for url, _depth in pending}
    crawled_keys: set[str] = set()
    discovered_keys: set[str] = set()
    discovered: list[str] = []
    crawl_log: list[dict[str, Any]] = []

    async with async_playwright() as p:
        browser = await _launch_browser(p)
        try:
            context = await browser.new_context(
                viewport={"width": 1366, "height": 900},
                reduced_motion="reduce",
                locale="en-US",
                user_agent=USER_AGENT,
            )
            await context.route("**/*", _block_discovery_assets)
            try:
                while pending and len(discovered) < max_urls:
                    batch: list[tuple[str, int]] = []
                    while pending and len(batch) < concurrency:
                        url, depth = pending.pop(0)
                        key = _url_key(url)
                        if key in crawled_keys or depth > max_depth:
                            continue
                        crawled_keys.add(key)
                        batch.append((url, depth))
                    if not batch:
                        continue

                    results = await asyncio.gather(
                        *(_crawl_one_page(context, url, timeout_ms, wait_ms) for url, _depth in batch)
                    )
                    for (url, depth), result in zip(batch, results, strict=False):
                        final_url = result.get("final_url") or url
                        try:
                            final_url = normalize_url(str(final_url))
                        except RestedCaptureError:
                            final_url = url

                        if _is_same_site(final_url, allowed_site_keys) and _looks_like_page_url(final_url):
                            final_key = _url_key(final_url)
                            if final_key not in discovered_keys:
                                discovered_keys.add(final_key)
                                discovered.append(final_url)

                        crawl_log.append({
                            "url": url,
                            "final_url": final_url,
                            "depth": depth,
                            "ok": bool(result.get("ok")),
                            "status": result.get("status"),
                            "link_count": len(result.get("links") or []),
                            "elapsed_ms": result.get("elapsed_ms"),
                            "error": result.get("error"),
                        })

                        if depth >= max_depth:
                            continue
                        links = same_site_page_urls(final_url, list(result.get("links") or []), allowed_site_keys)
                        for link in links:
                            if len(discovered) + len(pending) >= max_urls:
                                break
                            key = _url_key(link)
                            if key in queued_keys or key in crawled_keys:
                                continue
                            queued_keys.add(key)
                            pending.append((link, depth + 1))
            finally:
                await context.close()
        finally:
            await browser.close()

    return discovered, crawl_log


def discover_site_urls(
    seed_values: list[str],
    explicit_sitemap_urls: list[str] | None = None,
    *,
    max_urls: int = MAX_CRAWL_URLS,
    max_depth: int = DEFAULT_CRAWL_DEPTH,
    concurrency: int = DEFAULT_CRAWL_CONCURRENCY,
    timeout_ms: int = DEFAULT_DISCOVERY_TIMEOUT_MS,
    wait_ms: int = DEFAULT_DISCOVERY_WAIT_MS,
    include_sitemaps: bool = True,
) -> tuple[list[str], dict[str, Any]]:
    seeds = _dedupe_urls(seed_values)
    if not seeds:
        raise RestedCaptureError("add a site URL to crawl")

    max_urls = max(1, min(MAX_CRAWL_URLS, int(max_urls or MAX_CRAWL_URLS)))
    max_depth = max(0, min(20, int(max_depth or DEFAULT_CRAWL_DEPTH)))
    concurrency = max(1, min(12, int(concurrency or DEFAULT_CRAWL_CONCURRENCY)))
    timeout_ms = max(2000, min(60000, int(timeout_ms or DEFAULT_DISCOVERY_TIMEOUT_MS)))
    wait_ms = max(0, min(5000, int(wait_ms or DEFAULT_DISCOVERY_WAIT_MS)))
    allowed_site_keys = {_site_host_key(urlparse(url).hostname) for url in seeds}

    sitemap_urls = _dedupe_urls(explicit_sitemap_urls or [])
    sitemap_sources: list[str] = []
    sitemap_pages: list[str] = []
    sitemap_errors: list[dict[str, str]] = []
    if include_sitemaps:
        for seed in seeds:
            sitemap_urls.extend(_sitemap_hints_for_seed(seed))
        for sitemap_url in _dedupe_urls(sitemap_urls):
            try:
                pages = expand_sitemap(sitemap_url, limit=max_urls)
            except Exception as exc:
                sitemap_errors.append({"url": sitemap_url, "error": str(exc)})
                continue
            filtered = [
                page for page in pages
                if _is_same_site(page, allowed_site_keys) and _looks_like_page_url(page)
            ]
            if filtered:
                sitemap_sources.append(sitemap_url)
                sitemap_pages.extend(filtered)

    crawl_seeds = _dedupe_urls([*seeds, *sitemap_pages])
    crawled, crawl_log = asyncio.run(
        _discover_site_urls_async(
            crawl_seeds,
            allowed_site_keys,
            max_urls,
            max_depth,
            concurrency,
            timeout_ms,
            wait_ms,
        )
    )
    urls = _dedupe_urls([*crawled, *sitemap_pages, *seeds])
    if len(urls) > max_urls:
        urls = urls[:max_urls]
    return urls, {
        "mode": "site",
        "seed_urls": seeds,
        "page_count": len(urls),
        "max_urls": max_urls,
        "max_depth": max_depth,
        "concurrency": concurrency,
        "include_sitemaps": include_sitemaps,
        "sitemap_sources": sitemap_sources,
        "sitemap_errors": sitemap_errors[:20],
        "crawl_log": crawl_log,
    }


def build_capture_plan(
    payload: dict[str, Any], *, allow_map_files: bool = False,
    allow_loopback: bool = False, allow_private_hosts: tuple[str, ...] = (),
) -> tuple[list[str], list[CaptureTarget], dict[str, Any]]:
    if type(allow_map_files) is not bool:
        raise RestedCaptureError("allow_map_files must be a bool from the call site")
    if type(allow_loopback) is not bool:
        raise RestedCaptureError("allow_loopback must be a bool from the call site")
    if any(key in payload for key in ("_allow_map_files", "_allow_loopback", "_allow_private_hosts")):
        raise RestedCaptureError("_allow_* capabilities are call-site arguments, not payload keys")
    allow_private_hosts = tuple(_canonical_host(host) for host in allow_private_hosts)

    # The input seam: exactly one of the plain URL family or one map input.
    map_inputs = [
        ("manifest_path", payload.get("manifest_path")),
        ("map_path", payload.get("map_path")),
        ("manifest_text", payload.get("manifest_text")),
        ("map_text", payload.get("map_text")),
    ]
    supplied_maps = [name for name, value in map_inputs if value is not None]
    url_keys = ("url", "urls", "sitemap_url", "crawl_url", "local_path", "crawl", "local")
    url_supplied = bool(
        payload.get("url")
        or payload.get("urls")
        or payload.get("sitemap_url")
        or payload.get("crawl_url")
        or payload.get("local_path")
        or payload.get("crawl")
        or payload.get("local")
    )
    # Declared-but-empty is a URL error, not a missing-source error: a caller
    # who asked for URL capture with nothing in it hears the URL complaint.
    url_declared = any(payload.get(key) is not None for key in url_keys)
    source_names = (["url"] if url_supplied else []) + supplied_maps
    if not source_names:
        raise RestedCaptureError("add at least one URL")
    if len(source_names) > 1:
        raise PathwayMapError(
            "provide a manifest or a map, not both — got " + ", ".join(sorted(source_names))
        )

    # Imported here (not at module top) to keep the capture/pathway module
    # pair free of an import cycle.
    from .pathway_plan import MAX_TARGETS, plan_targets_from_map

    max_targets = MAX_TARGETS
    if supplied_maps:
        try:
            max_targets = int(payload.get("max_targets", MAX_TARGETS))
        except (TypeError, ValueError):
            raise RestedCaptureError("max_targets must be a number") from None
        if max_targets < 1:
            raise RestedCaptureError("max_targets must be at least 1")

    def capture_matrix() -> tuple[list[str], list[str]]:
        viewport_ids = payload.get("viewports") or list(DEFAULT_VIEWPORTS)
        if not isinstance(viewport_ids, list):
            raise RestedCaptureError("viewports must be a list")
        viewport_ids = [str(v).strip().lower() for v in viewport_ids if str(v).strip()]
        if not viewport_ids:
            raise RestedCaptureError("choose at least one viewport")
        for viewport_id in viewport_ids:
            if not DEFAULT_VIEWPORTS.get(viewport_id):
                raise RestedCaptureError(f"unknown viewport: {viewport_id}")

        schemes = payload.get("schemes") or list(DEFAULT_SCHEMES)
        if isinstance(schemes, str):
            schemes = [schemes]
        if not isinstance(schemes, list):
            raise RestedCaptureError("schemes must be a list")
        schemes = [str(s).strip().lower() for s in schemes if str(s).strip()]
        if not schemes:
            raise RestedCaptureError("choose at least one color scheme")
        for scheme in schemes:
            if scheme not in {"light", "dark", "no-preference"}:
                raise RestedCaptureError(f"unknown color scheme: {scheme}")
        return viewport_ids, schemes

    # Map mode validates before planning; legacy validates after discovery.
    if supplied_maps:
        viewport_ids, schemes = capture_matrix()

    pathway_targets: list[CaptureTarget] | None = None
    if supplied_maps:
        base_url = str(payload.get("base_url") or "").strip()
        if not base_url:
            raise RestedCaptureError("base_url is required when feeding a pathway map")
        if any(payload.get(key) is not None for key in ("manifest_path", "map_path")) and not allow_map_files:
            raise RestedCaptureError("map file paths are CLI-only; pass map_text or manifest_text")
        pathway_map = load_pathway_map(
            manifest_path=payload.get("manifest_path"),
            map_path=payload.get("map_path"),
            manifest_text=payload.get("manifest_text"),
            map_text=payload.get("map_text"),
        )
        urls, pathway_targets = plan_targets_from_map(
            pathway_map,
            base_url,
            max_targets=max_targets,
            # M-expanded-targets: the planner refuses the EXPANDED matrix
            # (pathways x viewports x schemes) before any navigation.
            viewport_count=len(viewport_ids),
            scheme_count=len(schemes),
            # D92: a dry-run plan is built with no navigation and no network
            # call — never a page read to bind a named control.
            dry_run=bool(payload.get("dry_run")),
            allow_loopback=allow_loopback,
            allow_private_hosts=allow_private_hosts,
        )
        crawl_enabled = False
        local_enabled = False
        discovery: dict[str, Any] = {
            "mode": "map",
            "pathway_count": len(pathway_map.get("pathways") or []),
            "route_count": len(pathway_map.get("routes") or []),
            "manifest_hash": pathway_map.get("hash"),
        }
    else:
        raw_urls = payload.get("urls") or []
        if isinstance(raw_urls, str):
            raw_urls = re.split(r"[\r\n,]+", raw_urls)
        if not isinstance(raw_urls, list):
            raise RestedCaptureError("urls must be a list or newline text")

        urls = [str(item).strip() for item in raw_urls if str(item).strip()]
        if payload.get("url"):
            urls.append(str(payload.get("url")).strip())
        sitemap_url = str(payload.get("sitemap_url") or "").strip()
        crawl_url = str(payload.get("crawl_url") or "").strip()
        local_path = str(payload.get("local_path") or "").strip()
        crawl_enabled = bool(payload.get("crawl") or crawl_url)
        local_enabled = bool(payload.get("local") or local_path)
        discovery = {"mode": "manual", "page_count": 0}
        if crawl_url:
            urls.append(crawl_url)

        if local_enabled:
            urls, discovery = discover_local_html(
                local_path or (urls[0] if urls else ""),
                max_urls=int(payload.get("max_pages") or payload.get("max_urls") or MAX_CRAWL_URLS),
                max_depth=int(payload.get("crawl_depth") or DEFAULT_CRAWL_DEPTH),
            )
        elif crawl_enabled:
            urls, discovery = discover_site_urls(
                urls,
                explicit_sitemap_urls=[sitemap_url] if sitemap_url else [],
                max_urls=int(payload.get("max_pages") or payload.get("max_urls") or MAX_CRAWL_URLS),
                max_depth=int(payload.get("crawl_depth") or DEFAULT_CRAWL_DEPTH),
                concurrency=int(payload.get("crawl_concurrency") or DEFAULT_CRAWL_CONCURRENCY),
                timeout_ms=int(payload.get("discovery_timeout_ms") or DEFAULT_DISCOVERY_TIMEOUT_MS),
                wait_ms=int(payload.get("discovery_wait_ms") or DEFAULT_DISCOVERY_WAIT_MS),
                include_sitemaps=bool(payload.get("include_sitemaps", True)),
            )
        elif sitemap_url:
            urls.extend(expand_sitemap(sitemap_url))

        urls = _dedupe_urls(urls)

        if not urls:
            raise RestedCaptureError("add at least one URL")
        if len(urls) > MAX_URLS:
            raise RestedCaptureError(f"too many URLs; maximum is {MAX_URLS}")
        viewport_ids, schemes = capture_matrix()

    targets: list[CaptureTarget] = []
    if pathway_targets is not None:
        # Map-driven: every pathway target expands across the same
        # viewport x scheme matrix a URL-driven plan builds, so the two plans
        # are one shape.
        for pathway_target in pathway_targets:
            for viewport_id in viewport_ids:
                viewport = DEFAULT_VIEWPORTS.get(viewport_id)
                if not viewport:
                    raise RestedCaptureError(f"unknown viewport: {viewport_id}")
                for scheme in schemes:
                    if scheme not in {"light", "dark", "no-preference"}:
                        raise RestedCaptureError(f"unknown color scheme: {scheme}")
                    targets.append(
                        replace(
                            pathway_target,
                            viewport_id=viewport_id,
                            width=int(viewport["width"]),
                            height=int(viewport["height"]),
                            scheme=scheme,
                        )
                    )
    else:
        for url_index, url in enumerate(urls, start=1):
            for viewport_id in viewport_ids:
                viewport = DEFAULT_VIEWPORTS.get(viewport_id)
                if not viewport:
                    raise RestedCaptureError(f"unknown viewport: {viewport_id}")
                for scheme in schemes:
                    if scheme not in {"light", "dark", "no-preference"}:
                        raise RestedCaptureError(f"unknown color scheme: {scheme}")
                    targets.append(
                        CaptureTarget(
                            index=url_index,
                            url=url,
                            viewport_id=viewport_id,
                            width=int(viewport["width"]),
                            height=int(viewport["height"]),
                            scheme=scheme,
                        )
                    )

    # M-expanded-targets: the defensive post-expansion check — the refusal
    # text says "capture targets", so the ceiling is the TARGET count.
    if pathway_targets is not None and len(targets) > max_targets:
        raise RestedCaptureError(f"too many capture targets; maximum is {max_targets}")

    # The plan says why a pathway is flagged. Status is read by the closed
    # set (WIRED/MOCKED/UNWIRED/INCOMPLETE/UNCLEAR/UNKNOWN), never by
    # substring; an UNWIRED pathway is planned AND flagged, never dropped.
    pathway_flags: list[dict[str, str]] = []
    if pathway_targets is not None:
        for pathway_target in pathway_targets:
            if pathway_target.pathway_status == "UNWIRED":
                pathway_flags.append({
                    "pathway_id": pathway_target.pathway_id,
                    "flag": "UNWIRED",
                    "reason": (
                        "the map marks this pathway UNWIRED — planned and kept, "
                        "but its control may not be wired to a real state"
                    ),
                })
        if bool(payload.get("dry_run")):
            from .pathway_plan import named_control_needs_the_page

            # D92: in dry-run nothing is bound from a page. A named control
            # the map hands no selector for is reported unbound (selector=-)
            # with a flag saying so — planned and kept, never silently dropped.
            for pathway, pathway_target in zip(
                pathway_map.get("pathways") or [], pathway_targets
            ):
                if not pathway_target.trigger_selector and named_control_needs_the_page(pathway):
                    pathway_flags.append({
                        "pathway_id": pathway_target.pathway_id,
                        "flag": "UNBOUND",
                        "reason": (
                            "the map names this control only by label or call — dry-run "
                            "launches no browser and makes no network call, so it stays "
                            "unbound (selector=-); run without --dry-run to bind it from "
                            "the page"
                        ),
                    })

    settings = {
        "allow_loopback": allow_loopback if supplied_maps else True,
        "loopback_host": (
            urlparse(base_url).hostname if supplied_maps and allow_loopback else None
        ),
        "allow_private_hosts": allow_private_hosts,
        "_navigation_cache": {},
        "format": str(payload.get("format") or "png").lower(),
        "full_page": bool(payload.get("full_page", True)),
        "timeout_ms": int(payload.get("timeout_ms") or DEFAULT_TIMEOUT_MS),
        "wait_ms": int(payload.get("wait_ms") or DEFAULT_WAIT_MS),
        "concurrency": max(1, min(8, int(payload.get("concurrency") or DEFAULT_CONCURRENCY))),
        "retries": max(0, min(3, int(payload.get("retries", DEFAULT_RETRIES)))),
        "retry_timeout_ms": max(
            int(payload.get("timeout_ms") or DEFAULT_TIMEOUT_MS),
            int(payload.get("retry_timeout_ms") or DEFAULT_RETRY_TIMEOUT_MS),
        ),
        "jpeg_quality": max(40, min(100, int(payload.get("jpeg_quality") or 88))),
        # Cookies for signed-in capture. This builder whitelists keys, so anything not
        # named here is silently dropped — which is why auth has to be threaded through
        # explicitly rather than just passed in the payload.
        "cookies": list(payload.get("cookies") or []),
        # Deterministic state sweep — click every mapped control and capture what it opens.
        "states": bool(payload.get("states", False)),
        "states_limit": max(1, min(60, int(payload.get("states_limit") or 20))),
        "state_wait_ms": max(200, int(payload.get("state_wait_ms") or 900)),
        "capture_count": len(targets),
        "crawl": crawl_enabled,
        "local": local_enabled,
        "page_count": len(urls),
        "pathway_flags": pathway_flags,
        "discovery": {**discovery, "page_count": len(urls)},
    }
    if settings["format"] not in {"png", "jpeg"}:
        raise RestedCaptureError("format must be png or jpeg")
    return urls, targets, settings


def _capture_file_name(target: CaptureTarget, fmt: str) -> str:
    parsed = urlparse(target.url)
    site = _slug(parsed.netloc + parsed.path, f"site-{target.index:03d}")
    ext = "jpg" if fmt == "jpeg" else "png"
    return f"{target.index:03d}-{site}-{target.viewport_id}-{target.scheme}.{ext}"


async def _settle_page(page: Any, wait_ms: int) -> None:
    try:
        await page.add_style_tag(content=REST_CSS)
    except Exception:
        pass
    try:
        await page.evaluate(
            """async () => {
              if (document.fonts && document.fonts.ready) await document.fonts.ready;
              for (const video of Array.from(document.querySelectorAll('video'))) {
                try { video.pause(); } catch (_) {}
              }
              window.scrollTo(0, 0);
            }"""
        )
    except Exception:
        pass
    await page.wait_for_timeout(wait_ms)


def _target_context_key(target: CaptureTarget) -> tuple[str, str, int, int, str]:
    parsed = urlparse(target.url)
    if parsed.scheme == "file":
        return ("local-file", target.viewport_id, target.width, target.height, target.scheme)
    return (_site_host_key(parsed.hostname), target.viewport_id, target.width, target.height, target.scheme)


# ── deterministic state sweep ────────────────────────────────────────────────
# A resting screenshot only ever shows a page with everything closed. Every modal,
# dropdown, tab panel, accordion and toggle is invisible to a page-level capture — which
# is most of what a designer actually needs. This maps the interactive controls, clicks
# each one, captures what it opens, then restores the baseline. No model involved: the
# DOM already says which elements are interactive.

_MAP_CONTROLS_JS = """() => {
  const vis = el => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' &&
           s.display !== 'none' && s.opacity !== '0';
  };
  const label = el => (el.innerText || el.getAttribute('aria-label') ||
                       el.getAttribute('title') || el.value || '')
                       .trim().replace(/\\s+/g, ' ').slice(0, 40);
  const SEL = [
    'button', '[role=button]', '[role=tab]', 'summary', 'details',
    '[aria-expanded]', '[aria-haspopup]', '[data-toggle]', '[data-modal]',
    '[class*=toggle]', '[class*=tab]', '[class*=accordion]', '[class*=dropdown]',
    '[class*=swatch]', '[class*=chip]', '[class*=menu-item]'
  ].join(',');
  const out = []; const seen = new Set();
  document.querySelectorAll(SEL).forEach((el, i) => {
    if (!vis(el)) return;
    const t = label(el);
    if (!t) return;
    // skip destructive / navigational controls that would end the sweep
    if (/^(sign out|log ?out|delete|remove|back|←)/i.test(t)) return;
    const key = t + '|' + el.tagName;
    if (seen.has(key)) return;
    seen.add(key);
    el.setAttribute('data-capturd-sweep', 'c' + i);
    out.push({ id: 'c' + i, label: t, tag: el.tagName,
               expands: el.getAttribute('aria-expanded') === 'false' ||
                        el.hasAttribute('aria-haspopup') });
  });
  return out;
}"""

_FINGERPRINT_JS = """() => {
  // A state change is not always textual: picking a theme swatch or flipping a toggle can
  // leave the copy identical while the page looks completely different. So fingerprint
  // text AND the signals that carry visual state.
  const t = (document.body.innerText || '').slice(0, 3000);
  const open = document.querySelectorAll(
    '[aria-expanded=true],[open],dialog[open],[role=dialog],[class*=modal]:not([hidden])').length;
  const root = document.documentElement;
  const themeBits = [root.getAttribute('data-theme'), root.className,
                     document.body.className, root.getAttribute('data-mode')].join(',');
  const selected = document.querySelectorAll(
    '[aria-selected=true],[aria-checked=true],[aria-current],.is-on,.is-active,.active,.selected').length;
  const bg = getComputedStyle(document.body).backgroundColor;
  const vis = document.querySelectorAll('body *').length;
  return [t.length, open, selected, vis, themeBits, bg, t.slice(0, 300)].join('|');
}"""


def _state_slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s or "state")[:40]


_DISMISS_LABELS = ("Skip intro", "Skip", "Got it", "Dismiss", "Close", "No thanks", "Maybe later")


async def _dismiss_overlays(page: Any) -> None:
    """Clear intro/splash overlays before sweeping.

    An intro video or welcome splash sits ON TOP of the app: every control underneath
    still reports visible and enabled, but the click is intercepted. The sweep then finds
    nothing and reports zero states, which looks like a page with no interactivity rather
    than a page behind a curtain.
    """
    for label in _DISMISS_LABELS:
        for _ in range(2):
            try:
                el = page.get_by_text(label, exact=True).first
                if await el.count() and await el.is_visible():
                    await el.click(timeout=1500, force=True)
                    await page.wait_for_timeout(700)
                else:
                    break
            except Exception:
                break


async def _sweep_states(page: Any, target: CaptureTarget, settings: dict[str, Any],
                        base_path: Path) -> list[dict[str, Any]]:
    """Click each mapped control, capture the state it opens, restore, and move on."""
    limit = int(settings.get("states_limit") or 20)
    shots: list[dict[str, Any]] = []
    await _dismiss_overlays(page)
    try:
        controls = await page.evaluate(_MAP_CONTROLS_JS)
    except Exception:
        return shots
    try:
        baseline = await page.evaluate(_FINGERPRINT_JS)
    except Exception:
        return shots
    seen = {baseline}
    base_url = page.url
    shot_kwargs = {"type": settings["format"], "animations": "disabled",
                   "full_page": settings["full_page"]}
    if settings["format"] == "jpeg":
        shot_kwargs["quality"] = settings["jpeg_quality"]

    for control in controls[:limit]:
        try:
            el = page.locator(f"[data-capturd-sweep={control['id']}]").first
            if await el.count() == 0 or not await el.is_visible():
                continue
            await el.click(timeout=2500)
            await page.wait_for_timeout(int(settings.get("state_wait_ms") or 900))
            fingerprint = await page.evaluate(_FINGERPRINT_JS)
            if fingerprint not in seen:
                seen.add(fingerprint)
                name = f"{base_path.stem}--open--{_state_slug(control['label'])}{base_path.suffix}"
                out = base_path.with_name(name)
                await page.screenshot(path=str(out), **shot_kwargs)
                shots.append({
                    "url": target.url, "state_id": target.state_id,
                    "viewport": target.viewport_id, "scheme": target.scheme,
                    "width": target.width, "height": target.height,
                    "status": "ok", "file": out.name, "bytes": out.stat().st_size,
                    "state": "open", "control": control["label"], "control_tag": control["tag"],
                })
            # restore: Escape, then re-click, then reload as the last resort
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(250)
            if await page.evaluate(_FINGERPRINT_JS) != baseline:
                try:
                    if await el.count() and await el.is_visible():
                        await el.click(timeout=1500)
                        await page.wait_for_timeout(250)
                except Exception:
                    pass
            if page.url != base_url or await page.evaluate(_FINGERPRINT_JS) != baseline:
                await page.goto(base_url, wait_until="domcontentloaded",
                                timeout=int(settings["timeout_ms"]))
                await _settle_page(page, settings["wait_ms"])
                await page.evaluate(_MAP_CONTROLS_JS)   # re-tag after reload
        except Exception:
            continue
    return shots


async def _apply_trigger(page: Any, selector: str, wait_ms: int) -> bool:
    """Click the pathway's named control to reach its rested state.

    The selector is the planner's candidate selector (never hand-rolled).
    A click is meaningful only when its URL or DOM fingerprint changes.
    A no-op stays baseline evidence. A missing control or a failed click is not a capture error — we shoot the
    loaded state and report ``trigger_fired`` False; the quality gate reads
    that flag to tell a baseline from a meaningful state shot.
    """
    sel = (selector or "").strip()
    if not sel:
        return False
    try:
        element = await page.query_selector(sel)
        if element is None:
            return False
        before_url = page.url
        before = await page.evaluate(_FINGERPRINT_JS)
        await element.click(timeout=TRIGGER_CLICK_TIMEOUT_MS)
        await _settle_page(page, wait_ms)
        after = await page.evaluate(_FINGERPRINT_JS)
        if page.url != before_url or after != before:
            return True
        deadline = time.monotonic() + TRIGGER_STATE_TIMEOUT_MS / 1000
        while time.monotonic() < deadline:
            await page.wait_for_timeout(100)
            after = await page.evaluate(_FINGERPRINT_JS)
            if page.url != before_url or after != before:
                return True
        return False
    except Exception as exc:
        warnings.warn(f"capture trigger failed ({type(exc).__name__}); trigger_fired=False",
                      RuntimeWarning, stacklevel=2)
        return False


async def _capture_one(
    context: Any, target: CaptureTarget, settings: dict[str, Any], path: Path,
    *, guard_errors: list[RestedCaptureError] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    attempts = max(1, int(settings.get("retries", DEFAULT_RETRIES)) + 1)
    last_error = ""
    navigation_cache = settings.get("_navigation_cache")
    if navigation_cache is None:
        navigation_cache = {}
        settings["_navigation_cache"] = navigation_cache
    allow_private_hosts = tuple(settings.get("allow_private_hosts") or ())
    for attempt in range(1, attempts + 1):
        page = None
        allow_loopback = bool(settings.get("allow_loopback"))
        loopback_host = settings.get("loopback_host")
        if allow_loopback and loopback_host is None:
            loopback_host = urlparse(target.url).hostname
        page_errors: list[RestedCaptureError] = []
        try:
            timeout_ms = int(settings["timeout_ms"])
            if attempt > 1:
                timeout_ms = max(timeout_ms, int(settings.get("retry_timeout_ms") or DEFAULT_RETRY_TIMEOUT_MS))
            page = await context.new_page()
            await _install_navigation_guard(
                page, allow_loopback=allow_loopback, loopback_host=loopback_host,
                allow_private_hosts=allow_private_hosts,
                navigation_cache=navigation_cache, guard_errors=page_errors,
            )
            page.set_default_timeout(timeout_ms)
            await _check_navigation_url(
                target.url, allow_loopback=allow_loopback, loopback_host=loopback_host,
                allow_private_hosts=allow_private_hosts, cache=navigation_cache,
            )
            if page_errors:
                raise page_errors[0]
            await page.goto(target.url, wait_until="domcontentloaded", timeout=timeout_ms)
            if page_errors:
                raise page_errors[0]
            await _check_navigation_url(
                page.url, allow_loopback=allow_loopback, loopback_host=loopback_host,
                allow_private_hosts=allow_private_hosts, cache=navigation_cache,
            )
            try:
                await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 15000))
            except Exception:
                pass
            await _settle_page(page, settings["wait_ms"])
            trigger_fired = False
            if (target.trigger_selector or "").strip():
                trigger_fired = await _apply_trigger(page, target.trigger_selector, settings["wait_ms"])
            screenshot_kwargs: dict[str, Any] = {
                "path": str(path),
                "type": settings["format"],
                "full_page": settings["full_page"],
                "animations": "disabled",
            }
            if settings["format"] == "jpeg":
                screenshot_kwargs["quality"] = settings["jpeg_quality"]
            if page_errors:
                raise page_errors[0]
            await _check_navigation_url(
                page.url, allow_loopback=allow_loopback, loopback_host=loopback_host,
                allow_private_hosts=allow_private_hosts, cache=navigation_cache,
            )
            await page.screenshot(**screenshot_kwargs)
            stat = path.stat()
            extra_states: list[dict[str, Any]] = []
            if settings.get("states"):
                extra_states = await _sweep_states(page, target, settings, path)
            if page_errors:
                raise page_errors[0]
            return {
                "url": target.url,
                "states": extra_states,
                "state_id": target.state_id,
                "viewport": target.viewport_id,
                "scheme": target.scheme,
                "width": target.width,
                "height": target.height,
                "status": "ok",
                "file": path.name,
                "bytes": stat.st_size,
                "trigger_selector": target.trigger_selector,
                "trigger_fired": trigger_fired,
                "attempts": attempt,
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            }
        except Exception as exc:
            selected_error = (
                page_errors[0] if page_errors
                else guard_errors[0] if guard_errors
                else exc
            )
            last_error = str(selected_error)
            if attempt < attempts:
                await asyncio.sleep(0.35)
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass

    return {
        "url": target.url,
        "state_id": target.state_id,
        "viewport": target.viewport_id,
        "scheme": target.scheme,
        "width": target.width,
        "height": target.height,
        "status": "failed",
        "error": last_error,
        "attempts": attempts,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


async def _launch_browser(playwright: Any, *, allow_file_access: bool = False) -> Any:
    launch_args = ["--allow-file-access-from-files"] if allow_file_access else []
    attempts: list[dict[str, Any]] = [{"headless": True, "args": launch_args}]
    if os.name == "nt":
        attempts.extend([
            {"headless": True, "channel": "msedge", "args": launch_args},
            {"headless": True, "channel": "chrome", "args": launch_args},
        ])

    last_error: Exception | None = None
    for kwargs in attempts:
        try:
            return await playwright.chromium.launch(**kwargs)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(str(last_error) if last_error else "unable to launch Chromium")


async def _run_capture_async(
    targets: list[CaptureTarget],
    settings: dict[str, Any],
    shots_dir: Path,
    progress: Any,
) -> list[dict[str, Any]]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Install engine requirements, then run "
            "`python -m playwright install chromium` if system Edge is unavailable."
        ) from exc

    sem = asyncio.Semaphore(settings["concurrency"])
    results: list[dict[str, Any]] = []

    async with async_playwright() as p:
        allow_file_access = any(urlparse(target.url).scheme == "file" for target in targets)
        browser = await _launch_browser(p, allow_file_access=allow_file_access)
        contexts: dict[tuple[str, str, int, int, str], Any] = {}
        context_lock = asyncio.Lock()

        async def context_for(target: CaptureTarget) -> Any:
            key = _target_context_key(target)
            async with context_lock:
                context = contexts.get(key)
                if context is None:
                    context = await browser.new_context(
                        viewport={"width": target.width, "height": target.height},
                        color_scheme=None if target.scheme == "no-preference" else target.scheme,
                        reduced_motion="reduce",
                        device_scale_factor=1,
                        locale="en-US",
                        user_agent=USER_AGENT,
                    )
                    # Signed-in capture: without cookies the batch can only ever shoot
                    # what a logged-out visitor sees, so authenticated products are
                    # impossible to screenshot.
                    cookies = settings.get("cookies") or []
                    if cookies:
                        await context.add_cookies(cookies)
                    contexts[key] = context
                return context

        try:
            async def run_one(target: CaptureTarget) -> dict[str, Any]:
                async with sem:
                    file_name = _capture_file_name(target, settings["format"])
                    context = await context_for(target)
                    result = await _capture_one(context, target, settings, shots_dir / file_name)
                    progress(result)
                    return result

            results = await asyncio.gather(*(run_one(target) for target in targets))
        finally:
            for context in contexts.values():
                try:
                    await context.close()
                except Exception:
                    pass
            await browser.close()

    return results


def run_capture(
    targets: list[CaptureTarget],
    settings: dict[str, Any],
    shots_dir: Path,
    progress: Any,
) -> list[dict[str, Any]]:
    return asyncio.run(_run_capture_async(targets, settings, shots_dir, progress))


def build_quality_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Map run_capture's results to the shape quality.summarize_capture_quality reads.

    One entry per shot file: the page shot is the rested ``baseline`` unless
    its named control fired (then it is a ``state``), and every swept state
    shot is a ``state``. The quality module only ever reads — this mapping
    invents no successes and renames no failure.
    """
    entries: list[dict[str, Any]] = []
    for result in results or []:
        if not isinstance(result, dict):
            continue
        ok = result.get("status") == "ok"
        captures: list[dict[str, Any]] = [{
            "kind": "state" if result.get("trigger_fired") else "baseline",
            "success": bool(ok),
        }]
        for state in result.get("states") or []:
            if isinstance(state, dict):
                captures.append({"kind": "state", "success": state.get("status") == "ok"})
        entries.append({"file": result.get("file") or "", "manifest": {"captures": captures}})
    return {"results": entries}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _zip_dir(source_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file() and path.resolve() != zip_path.resolve():
                zf.write(path, path.relative_to(source_dir))


class RestedCaptureManager:
    """In-process job store for rested website capture runs."""

    def __init__(self, root_dir: Path | None = None) -> None:
        self.root_dir = root_dir or (_app_data_dir() / "rested-captures")
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def start(
        self, payload: dict[str, Any], *, allow_map_files: bool = False,
        allow_loopback: bool = False, allow_private_hosts: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        urls, targets, settings = build_capture_plan(
            payload, allow_map_files=allow_map_files, allow_loopback=allow_loopback,
            allow_private_hosts=allow_private_hosts,
        )
        job_id = uuid.uuid4().hex[:12]
        job_name = _slug(str(payload.get("name") or "") or _job_name(), _job_name())
        work_dir = self.root_dir / job_id
        shots_dir = work_dir / "shots"
        export_dir_raw = str(payload.get("export_dir") or "").strip()
        export_mode = str(payload.get("export_mode") or "zip").lower()
        export_dir = Path(export_dir_raw).expanduser() if export_dir_raw else None

        job = {
            "ok": True,
            "job_id": job_id,
            "name": job_name,
            "status": "queued",
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "message": "Queued",
            "urls": urls,
            "total": len(targets),
            "completed": 0,
            "failed": 0,
            "results": [],
            "settings": settings,
            "discovery": settings.get("discovery") or {},
            "work_dir": str(work_dir),
            "output_dir": "",
            "zip_path": "",
            "zip_url": "",
        }
        with self._lock:
            self._jobs[job_id] = job

        thread = threading.Thread(
            target=self._run_job,
            args=(job_id, targets, settings, work_dir, shots_dir, export_dir, export_mode),
            daemon=False,
            name=f"rested-capture-{job_id}",
        )
        thread.start()
        return self.get(job_id)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            return dict(job)

    def zip_path(self, job_id: str) -> Path:
        job = self.get(job_id)
        zip_path = Path(str(job.get("zip_path") or ""))
        if not zip_path.is_file():
            raise FileNotFoundError(job_id)
        return zip_path

    def _patch(self, job_id: str, patch: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.update(patch)
            job["updated_at"] = utc_now_iso()

    def _record_result(self, job_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job["results"].append(result)
            job["completed"] = int(job.get("completed", 0)) + 1
            if result.get("status") != "ok":
                job["failed"] = int(job.get("failed", 0)) + 1
            job["message"] = f"{job['completed']} of {job['total']} captured"
            job["updated_at"] = utc_now_iso()

    def _run_job(
        self,
        job_id: str,
        targets: list[CaptureTarget],
        settings: dict[str, Any],
        work_dir: Path,
        shots_dir: Path,
        export_dir: Path | None,
        export_mode: str,
    ) -> None:
        try:
            shots_dir.mkdir(parents=True, exist_ok=True)
            self._patch(job_id, {"status": "running", "message": "Capturing"})
            results = run_capture(
                targets,
                settings,
                shots_dir,
                lambda result: self._record_result(job_id, result),
            )
            manifest = {
                "job_id": job_id,
                "captured_at": utc_now_iso(),
                "settings": settings,
                "urls": self.get(job_id).get("urls") or [],
                "discovery": settings.get("discovery") or {},
                "results": results,
            }
            _write_json(work_dir / "manifest.json", manifest)

            output_dir = work_dir
            if export_dir is not None and export_mode == "folder":
                output_dir = export_dir / Path(str(self.get(job_id)["name"])).name
                if output_dir.exists():
                    output_dir = export_dir / f"{output_dir.name}-{job_id}"
                shutil.copytree(work_dir, output_dir)

            zip_parent = export_dir if export_dir is not None else work_dir
            zip_parent.mkdir(parents=True, exist_ok=True)
            zip_path = zip_parent / f"{Path(str(self.get(job_id)['name'])).name}.zip"
            if zip_path.exists():
                zip_path = zip_parent / f"{zip_path.stem}-{job_id}.zip"
            _zip_dir(output_dir, zip_path)

            status = "done" if int(self.get(job_id)["failed"]) == 0 else "done_with_errors"
            self._patch(
                job_id,
                {
                    "status": status,
                    "message": "Done" if status == "done" else "Done with errors",
                    "output_dir": str(output_dir),
                    "zip_path": str(zip_path),
                    "zip_url": f"/api/rested-captures/jobs/{job_id}/download",
                },
            )
        except Exception as exc:
            self._patch(job_id, {"ok": False, "status": "failed", "message": str(exc)})
