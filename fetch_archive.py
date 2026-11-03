#!/usr/bin/env python3
"""Fetch the latest Wayback Machine snapshot of every page under a domain.

For each unique URL under DOMAIN, picks the most recent capture taken on or
before CUTOFF_DATE (HTTP 200 only), downloads the original (un-rewritten)
content, and packs everything into a .tar.gz together with an index file.

Usage:
    python fetch_archive.py example.com 2020-12-31 [--output-dir out]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, unquote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from waybackpy import WaybackMachineCDXServerAPI

USER_AGENT = "fetch-from-web-archive/1.0 (personal archival tool; waybackpy)"


# --------------------------------------------------------------------------- #
# Argument handling
# --------------------------------------------------------------------------- #
def normalize_domain(raw: str) -> str:
    d = raw.strip()
    d = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", d)  # strip scheme
    d = d.split("?")[0].split("#")[0].rstrip("/")
    if not d or not re.fullmatch(r"[A-Za-z0-9.\-:/_~%]+", d):
        raise ValueError(f"Invalid domain: {raw!r}")
    return d


def parse_cutoff(raw: str) -> datetime:
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Cutoff date must be YYYY-MM-DD, got {raw!r}") from exc


# --------------------------------------------------------------------------- #
# Index (CDX) retrieval
# --------------------------------------------------------------------------- #
def build_index(domain, cutoff, match_type, html_only, max_pages):
    """Return a list of the latest 200-status snapshot per unique URL."""
    filters = ["statuscode:200"]
    if html_only:
        filters.append("mimetype:text/html")

    cdx = WaybackMachineCDXServerAPI(
        url=domain,
        user_agent=USER_AGENT,
        end_timestamp=cutoff.strftime("%Y%m%d") + "235959",
        filters=filters,
        match_type=match_type,
        use_pagination=True,
        max_tries=5,
    )

    latest = {}  # urlkey -> snapshot
    seen = 0
    for snap in cdx.snapshots():
        seen += 1
        if seen % 5000 == 0:
            print(f"  ...scanned {seen} captures, {len(latest)} unique URLs", flush=True)
        key = snap.urlkey
        cur = latest.get(key)
        if cur is None or snap.timestamp > cur.timestamp:
            latest[key] = snap

    print(f"Scanned {seen} captures -> {len(latest)} unique URLs", flush=True)

    rows = [
        {
            "urlkey": s.urlkey,
            "timestamp": s.timestamp,
            "original": s.original,
            "mimetype": s.mimetype,
            "statuscode": s.statuscode,
            "digest": s.digest,
            "length": s.length,
        }
        for s in latest.values()
    ]
    rows.sort(key=lambda r: r["original"])
    if max_pages:
        rows = rows[:max_pages]
    return rows


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Global limiter: at most one request start per `min_interval` seconds."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.min_interval
        if delay:
            time.sleep(delay)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    retry = Retry(
        total=6,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
    return s


def raw_url(row) -> str:
    # The "id_" flag returns the original bytes without Wayback's HTML rewriting.
    return f"https://web.archive.org/web/{row['timestamp']}id_/{row['original']}"


def download(session, limiter, row, max_bytes):
    limiter.wait()
    url = raw_url(row)
    try:
        with session.get(url, timeout=(15, 120), stream=True) as r:
            if r.status_code != 200:
                return row, None, f"HTTP {r.status_code}"
            buf = io.BytesIO()
            for chunk in r.iter_content(chunk_size=1 << 16):
                buf.write(chunk)
                if buf.tell() > max_bytes:
                    return row, None, f"larger than {max_bytes} bytes, skipped"
            return row, buf.getvalue(), None
    except requests.RequestException as exc:
        return row, None, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# Path mapping
# --------------------------------------------------------------------------- #
_BAD = re.compile(r'[<>:"|?*\x00-\x1f\\]')


def archive_path(original: str, used: set) -> str:
    parts = urlsplit(original)
    host = (parts.hostname or "unknown-host").lower()
    path = unquote(parts.path or "/")
    segments = [_BAD.sub("_", s)[:120] for s in path.split("/") if s not in ("", ".", "..")]
    if path.endswith("/") or not segments:
        segments.append("index.html")
    if parts.query:
        h = hashlib.sha1(parts.query.encode()).hexdigest()[:8]
        last = segments[-1]
        stem, dot, ext = last.rpartition(".")
        segments[-1] = f"{stem}__q_{h}.{ext}" if dot else f"{last}__q_{h}"
    rel = "/".join([host] + segments)

    # Avoid collisions (e.g. http vs https variants of the same URL)
    candidate, n = rel, 1
    while candidate in used:
        stem, dot, ext = rel.rpartition(".")
        candidate = f"{stem}__{n}.{ext}" if dot else f"{rel}__{n}"
        n += 1
    used.add(candidate)
    return candidate


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("domain")
    ap.add_argument("cutoff_date", help="YYYY-MM-DD")
    ap.add_argument("--output-dir", default="output")
    ap.add_argument("--match-type", default="domain",
                    choices=["exact", "prefix", "host", "domain"],
                    help="domain = include subdomains (default)")
    ap.add_argument("--html-only", action="store_true", help="Only text/html pages")
    ap.add_argument("--max-pages", type=int, default=0, help="Limit pages (0 = no limit)")
    ap.add_argument("--max-file-mb", type=int, default=50)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.5,
                    help="Minimum seconds between requests (global)")
    ap.add_argument("--index-only", action="store_true", help="Build index, skip downloads")
    args = ap.parse_args()

    domain = normalize_domain(args.domain)
    cutoff = parse_cutoff(args.cutoff_date)
    tag = re.sub(r"[^A-Za-z0-9.-]+", "_", domain) + "_" + cutoff.strftime("%Y-%m-%d")

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    print(f"Building index for {domain} (cutoff {cutoff:%Y-%m-%d})...", flush=True)
    rows = build_index(domain, cutoff, args.match_type, args.html_only, args.max_pages)
    if not rows:
        print("No snapshots found.", file=sys.stderr)
        return 1

    # Assign archive paths up front so the index can reference them.
    used: set = set()
    for r in rows:
        r["archive_path"] = archive_path(r["original"], used)
        r["status"] = "pending"

    index_csv = out / f"{tag}_index.csv"

    def write_index():
        with index_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) + ["error"],
                               extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({**r, "error": r.get("error", "")})

    if args.index_only:
        write_index()
        print(f"Index written: {index_csv}")
        return 0

    archive = out / f"{tag}.tar.gz"
    session = make_session()
    limiter = RateLimiter(args.delay)
    max_bytes = args.max_file_mb * 1024 * 1024
    ok = failed = 0
    batch_size = max(args.workers * 8, 32)

    with tarfile.open(archive, "w:gz") as tar, \
            ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            futures = [pool.submit(download, session, limiter, r, max_bytes) for r in batch]
            for fut in as_completed(futures):
                row, data, err = fut.result()
                if data is None:
                    row["status"], row["error"] = "failed", err
                    failed += 1
                    continue
                info = tarfile.TarInfo(name=f"{tag}/{row['archive_path']}")
                info.size = len(data)
                info.mtime = int(
                    datetime.strptime(row["timestamp"], "%Y%m%d%H%M%S")
                    .replace(tzinfo=timezone.utc).timestamp()
                )
                tar.addfile(info, io.BytesIO(data))
                row["status"] = "ok"
                ok += 1
            print(f"  {min(i + batch_size, len(rows))}/{len(rows)} processed "
                  f"(ok={ok}, failed={failed})", flush=True)

        # Add index + manifest inside the archive too
        write_index()
        tar.add(index_csv, arcname=f"{tag}/_index.csv")
        manifest = json.dumps({
            "domain": domain,
            "cutoff_date": cutoff.strftime("%Y-%m-%d"),
            "match_type": args.match_type,
            "html_only": args.html_only,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_urls": len(rows), "downloaded": ok, "failed": failed,
        }, indent=2).encode()
        info = tarfile.TarInfo(name=f"{tag}/_manifest.json")
        info.size = len(manifest)
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(manifest))

    size_mb = archive.stat().st_size / 1e6
    print(f"\nDone: {ok} downloaded, {failed} failed -> {archive} ({size_mb:.1f} MB)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
