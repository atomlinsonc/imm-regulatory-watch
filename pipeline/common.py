"""Shared helpers for the pipeline: polite cached downloads, HTML cleanup, JSON I/O."""
import hashlib
import html
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "cache"
RAW = ROOT / "data" / "raw"
CURATED = ROOT / "data" / "curated"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
# Seconds between live requests. These are public agency sites; one request a second is plenty.
DELAY = 1.0
_last_request = 0.0


class FetchError(Exception):
    pass


class TooLarge(FetchError):
    pass


def cache_path(url, suffix=""):
    return CACHE / (hashlib.sha1(url.encode()).hexdigest()[:20] + suffix)


def fetch(url, max_age_hours=20, suffix="", max_bytes=None):
    """Download url to the cache (unless a fresh copy exists) and return the cached path.
    max_bytes stops the download of anything larger, so oversized files are never pulled."""
    global _last_request
    path = cache_path(url, suffix)
    if path.exists() and path.stat().st_size > 0:
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if max_age_hours is None or age_hours < max_age_hours:
            return path
    CACHE.mkdir(parents=True, exist_ok=True)
    wait = DELAY - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    tmp = path.with_name(path.name + ".part")
    command = ["curl", "-sS", "-L", "--max-time", "90", "-A", USER_AGENT, "-o", str(tmp), "-w", "%{http_code}"]
    if max_bytes:
        command += ["--max-filesize", str(max_bytes)]
    result = subprocess.run(command + [url], capture_output=True, text=True)
    _last_request = time.time()
    status = result.stdout.strip()
    if result.returncode == 63 or (max_bytes and tmp.exists() and tmp.stat().st_size > max_bytes):
        tmp.unlink(missing_ok=True)
        raise TooLarge(f"{url} is larger than {max_bytes} bytes")
    if result.returncode != 0 or not status.startswith("2"):
        tmp.unlink(missing_ok=True)
        raise FetchError(f"{url} -> HTTP {status or '?'} {result.stderr.strip()[:200]}")
    tmp.replace(path)
    return path


def fetch_text(url, max_age_hours=20):
    return fetch(url, max_age_hours).read_text(encoding="utf-8", errors="replace")


def clean(fragment):
    """Strip tags and collapse whitespace."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def strip_scripts(page):
    return re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)


def table_rows(page):
    """Return each <tr> as a list of (cell_text, cell_html)."""
    rows = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)
        if cells:
            rows.append([(clean(c), c) for c in cells])
    return rows


def us_date(text):
    """'9/18/2026' -> '2026-09-18' (returns None if it does not parse)."""
    try:
        return datetime.strptime(text.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def save(name, data, folder=RAW):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return path


def load(name, folder=RAW, default=None):
    path = folder / name
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def now_stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def record_status(source, ok, detail=""):
    """Note whether a source was reached on this run. The page shows this, so a blocked
    source is visible instead of silently showing old data."""
    status = load("status.json", default={}) or {}
    entry = status.get(source, {})
    entry.update(last_attempt=now_stamp(), ok=bool(ok), detail=str(detail)[:240])
    if ok:
        entry["last_ok"] = now_stamp()
    status[source] = entry
    save("status.json", status)
