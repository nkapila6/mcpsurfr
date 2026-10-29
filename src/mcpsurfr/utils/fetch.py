#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on 2025-06-04 22:55:29 Wednesday

@author: Nikhil Kapila
"""

import logging, re, threading, time

from typing import List, Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import primp

from importlib.resources import as_file
from urllib.parse import quote

from .adapters import site_doc
from . import docsmd
from .cache import TTLCache
from .extract import extract_blocks, extract_main_text, page_meta, url_date
from .pdf import pdf_blocks

# stdout is the MCP protocol channel in stdio mode, so never print(); log to stderr
log = logging.getLogger(__name__)

# primp's text_plain (the fallback) draws HTML tables with box characters,
# which eat the 10k budget on table-heavy pages
_BOX_CHARS = re.compile(r"[─-╿]+")
_TEXT_TYPES = ("text/html", "application/xhtml", "text/plain")

def get_path_str(resource)->str:
    with as_file(resource) as path:
        return str(path)
    
# cached: loading takes ~50ms, and the embedder keeps per-shape plans warm across searches
@lru_cache(maxsize=1)
def fetch_embedder(path:str, l2_normalize:bool=True):
    from .embedder import TextEmbedder
    return TextEmbedder(path, l2_normalize=l2_normalize)

# one client per fetch thread: a shared client serializes concurrent requests
# (5 pages took 2x longer), but reusing one per thread keeps connections alive
# across calls (sequential fetches ~35% faster). Rebuilt every so often so the
# random browser fingerprint and cookies don't stick around forever.
_local = threading.local()
_CLIENT_USES = 50

def _client() -> "primp.Client":
    if getattr(_local, "uses", _CLIENT_USES) >= _CLIENT_USES:
        # impersonating a real browser gets past "set a user agent" walls (Wikipedia,
        # GitHub) that served plain requests a stub page
        _local.client = primp.Client(impersonate="random", follow_redirects=True)
        _local.uses = 0
    _local.uses += 1
    return _local.client

def _get(url: str, timeout: int) -> Tuple[Optional["primp.Response"], Optional[str]]:
    """Download a text page. Returns (response, None), or (None, why it failed)."""
    try:
        start_time = time.time()
        response = _client().get(url, timeout=timeout)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "text/html").lower()
        if not content_type.startswith(_TEXT_TYPES) and not _is_pdf(response):
            log.info("Skipping %s: %s", url, content_type)
            return None, f"not a web page ({content_type.split(';')[0]})"
        log.debug("Fetched %s in %.2fs", url, time.time() - start_time)
        return response, None
    except primp.StatusError as e:
        log.warning("Error fetching %s: HTTP %s", url, e.status_code)
        return None, f"HTTP {e.status_code}"
    except primp.TimeoutError:
        log.warning("Timed out fetching %s", url)
        return None, "timed out"
    except (primp.ConnectError, primp.DNSError, primp.DNSTimeoutError) as e:
        log.warning("Couldn't connect to %s: %s", url, e)
        return None, "couldn't connect"
    except primp.PrimpError as e:
        log.warning("Error fetching %s: %s - %s", url, type(e).__name__, e)
        return None, type(e).__name__

def _is_pdf(response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    if content_type.startswith("application/pdf"):
        return True
    # some servers send PDFs as a generic download, so check the magic bytes
    return content_type.startswith(("application/octet-stream", "binary/octet-stream")) \
        and response.content[:5] == b"%PDF-"

def _is_html(response) -> bool:
    return not response.headers.get("content-type", "text/html").lower().startswith("text/plain")

def _plain(response) -> str:
    return " ".join(_BOX_CHARS.sub(" ", response.text_plain).split())

def _readable(text: str) -> bool:
    """False for pages that came out as nav scraps only (JS-rendered sites, login
    walls): too short, or hardly any line reads like a sentence."""
    if len(text) < 200:
        return False
    prose = sum(len(line) for line in text.split("\n") if len(line.split()) >= 8)
    return prose >= 500 or prose >= 0.2 * len(text)

# the full-page and passage tools both read from here, so a page fetched by one
# call is free for the next for a few minutes
_docs = TTLCache(maxsize=64)
_UNREADABLE = "no readable content (nav only or rendered by JavaScript)"
# what a page carries besides its text, passed on to pages and passages in results
META_KEYS = ("title", "date", "archived", "archive_url")
# pages that are gone, not refused: only these go to the Wayback Machine. A 403 or 429
# is a site saying no, and the archive isn't a way around that.
_GONE = ("HTTP 404", "HTTP 410", "couldn't connect", "timed out")

def fetch_doc(url: str, timeout: int = 5) -> Dict:
    """A page as {"url", "blocks", "text"}, or {"url", "reason"} when it's unusable.
    "blocks" are (heading, text) pairs for passage search, "text" the main content.
    A page that's gone comes from the Wayback Machine instead, marked "archived"."""
    doc = _docs.get(url)
    if doc is None:
        doc = _fetch_doc(url, timeout)
        if doc.get("reason") in _GONE:
            doc = _from_archive(url, timeout) or doc
        if "text" in doc:
            _docs.set(url, doc)
    return doc

# the archive rate-limits hard (a 429 after a handful of lookups); once it does, leave
# it alone for a while instead of adding a doomed request to every dead page
_ARCHIVE_BACKOFF = 600
_archive_blocked_until = 0.0

def _from_archive(url: str, timeout: int) -> Optional[Dict]:
    """The closest Wayback Machine snapshot of a page that's gone, read like any page.
    Gives up quietly when the archive has none or is rate-limiting."""
    global _archive_blocked_until
    if time.monotonic() < _archive_blocked_until:
        return None
    timeout = max(timeout, 10)  # the archive is slower than most sites
    try:
        r = _client().get(f"https://archive.org/wayback/available?url={quote(url, safe='')}", timeout=timeout)
        if r.status_code == 429:
            _archive_blocked_until = time.monotonic() + _ARCHIVE_BACKOFF
            log.info("Wayback Machine is rate-limiting; not asking it for %ss", _ARCHIVE_BACKOFF)
            return None
        r.raise_for_status()
        snap = r.json().get("archived_snapshots", {}).get("closest") or {}
    except Exception as e:
        log.info("No archive lookup for %s: %s", url, e)
        return None
    if not snap.get("available") or str(snap.get("status")) != "200" or not snap.get("timestamp"):
        return None
    ts = snap["timestamp"]
    # id_ asks for the page as captured, without the archive's toolbar and rewritten links
    doc = _fetch_doc(f"https://web.archive.org/web/{ts}id_/{url}", timeout)
    if "text" not in doc:
        return None
    return doc | {"url": url, "archived": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}", "archive_url": snap.get("url", "")}

def _fetch_doc(url: str, timeout: int) -> Dict:
    doc = site_doc(url, _client(), timeout)
    if doc:
        return doc
    # a docs host already known to publish markdown copies: skip its HTML
    if docsmd.known_host(url) and docsmd.md_url(url):
        doc = docsmd.fetch(_client(), docsmd.md_url(url), url, timeout)
        if doc:
            return doc
    response, reason = _get(url, timeout)
    if response is None:
        return {"url": url, "reason": reason}
    if _is_pdf(response):
        blocks, reason = pdf_blocks(response.content)
        if not blocks:
            return {"url": url, "reason": reason}
        return {"url": url, "blocks": blocks, "text": "\n".join(f"[{page}] {text}" for page, text in blocks)}
    html = _is_html(response)
    md = docsmd.from_html(response.text, url) if html else None
    if md:
        doc = docsmd.fetch(_client(), md, url, timeout)
        docsmd.remember(url, doc is not None)
        if doc:
            # the HTML still knows the publish date, and often a better title
            return doc | page_meta(response.text)
    # primp decodes the charset (header or <meta>); extraction drops nav menus,
    # banners and footers. primp's own html->text is the fallback.
    text = (extract_main_text(response.text) if html else None) or _plain(response)
    blocks = extract_blocks(response.text) if html else []
    text_ok = _readable(text)
    blocks_ok = bool(blocks) and _readable("\n".join(t for _, t in blocks))
    if not text_ok and not blocks_ok:
        return {"url": url, "reason": _UNREADABLE}
    # each view stands in for the other when only one came out usable
    if not blocks_ok:
        blocks = [("", line) for line in text.split("\n") if line.strip()]
    if not text_ok:
        text = "\n".join(t for _, t in blocks)
    doc = {"url": url, "blocks": blocks, "text": text} | (page_meta(response.text) if html else {})
    if "date" not in doc and url_date(url):
        doc["date"] = url_date(url)
    return doc

def fetch_page(url: str, timeout: int = 5) -> Dict:
    """A page's main text: {"url", "text"}, or {"url", "reason"} when it's unusable."""
    doc = fetch_doc(url, timeout)
    return {k: v for k, v in doc.items() if k != "blocks"}

def fetch_blocks(url: str, timeout: int = 5) -> Tuple[List[Tuple[str, str]], Optional[str]]:
    """A page as (heading, text) blocks for passage search, plus why it failed if it did."""
    doc = fetch_doc(url, timeout)
    return doc.get("blocks", []), doc.get("reason")

# lives as long as the server so its threads, and their clients, carry over between calls
_pool = ThreadPoolExecutor(max_workers=5, thread_name_prefix="fetch")

def _parallel(fn, urls: List[str], failed) -> List:
    """fn over urls on 5 threads, in order. A page that crashes extraction becomes failed(url, reason)."""
    def safe(url):
        try:
            return fn(url)
        except Exception as e:
            log.warning("Fetching %s failed: %s", url, e)
            return failed(url, f"error: {type(e).__name__}")
    return list(_pool.map(safe, urls))

def fetch_all_blocks(urls: List[str]) -> Tuple[Dict[str, List[Tuple[str, str]]], List[Dict], Dict[str, Dict]]:
    """Fetch pages as blocks in parallel. Returns {url: blocks}, the skipped pages, and
    {url: {"title", "date"}} for the pages that have them."""
    pages, skipped, meta = {}, [], {}
    for url, doc in zip(urls, _parallel(fetch_doc, urls, lambda u, r: {"url": u, "reason": r})):
        if doc.get("blocks"):
            pages[url] = doc["blocks"]
            meta[url] = {k: doc[k] for k in META_KEYS if doc.get(k)}
        else:
            skipped.append({"url": url, "reason": doc.get("reason")})
    texts = drop_near_duplicates([{"url": u, "text": "\n".join(t for _, t in b)} for u, b in pages.items()], skipped)
    return {p["url"]: pages[p["url"]] for p in texts}, skipped, meta

def fetch_all_content(urls: List[str], budget: int) -> Tuple[List[Dict], List[Dict]]:
    """Fetch pages in parallel, best first. Returns the pages that fit the shared
    character budget plus the ones skipped, each with a reason."""
    pages, skipped = [], []
    for page in _parallel(fetch_page, urls, lambda u, r: {"url": u, "reason": r}):
        (pages if "text" in page else skipped).append(page)
    return fit_budget(drop_near_duplicates(pages, skipped), budget), skipped

# --- near-duplicates ---------------------------------------------------------
# the same page often lives at several URLs (docs/10 vs docs/current, mirrors,
# syndicated news). Compare the text, not the URL.

def _shingles(text: str, n: int = 5) -> set:
    words = text.lower().split()
    return {hash(tuple(words[i:i + n])) for i in range(max(1, len(words) - n + 1))}

def drop_near_duplicates(pages: List[Dict], skipped: List[Dict], threshold: float = 0.8) -> List[Dict]:
    """Keep the first of any pages that share most of their 5-word runs. Overlap is
    measured against the smaller page, so a mirror with extra chrome still counts."""
    kept, sets = [], []
    for page in pages:
        sh = _shingles(page["text"])
        dup = next((k for k, ks in zip(kept, sets) if len(sh & ks) > threshold * min(len(sh), len(ks))), None)
        if dup:
            skipped.append({"url": page["url"], "reason": f"near-duplicate of {dup['url']}"})
        else:
            kept.append(page)
            sets.append(sh)
    return kept

# --- budget ------------------------------------------------------------------

def fit_budget(pages: List[Dict], budget: int) -> List[Dict]:
    """Share one character budget across pages: short pages take what they need and
    the rest is split among the long ones. Cut pages get "truncated" and their full size."""
    alloc, left = {}, budget
    by_size = sorted(range(len(pages)), key=lambda i: len(pages[i]["text"]))
    for n, i in enumerate(by_size):
        alloc[i] = min(len(pages[i]["text"]), left // (len(pages) - n))
        left -= alloc[i]
    out = []
    for i, page in enumerate(pages):
        text = page["text"]
        entry = {"type": "text", "text": clip(text, alloc[i]), "url": page["url"]}
        entry.update({k: page[k] for k in META_KEYS if page.get(k)})
        if len(entry["text"]) < len(text):
            entry.update(truncated=True, chars=len(text))
        out.append(entry)
    return out

def clip(text: str, limit: int) -> str:
    """Cut to limit at a paragraph or sentence end if one is near, else at a space."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    floor = int(limit * 0.85)
    for sep in ("\n", ". ", "。", "! ", "? ", " "):
        pos = cut.rfind(sep, floor)
        if pos > 0:
            return cut[:pos + len(sep.strip())].rstrip()
    return cut
