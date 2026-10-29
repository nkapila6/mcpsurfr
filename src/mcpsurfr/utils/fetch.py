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

from .cache import TTLCache
from .extract import extract_blocks, extract_main_text
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

def fetch_doc(url: str, timeout: int = 5) -> Dict:
    """A page as {"url", "blocks", "text"}, or {"url", "reason"} when it's unusable.
    "blocks" are (heading, text) pairs for passage search, "text" the main content."""
    doc = _docs.get(url)
    if doc is None:
        doc = _fetch_doc(url, timeout)
        if "text" in doc:
            _docs.set(url, doc)
    return doc

def _fetch_doc(url: str, timeout: int) -> Dict:
    response, reason = _get(url, timeout)
    if response is None:
        return {"url": url, "reason": reason}
    if _is_pdf(response):
        blocks, reason = pdf_blocks(response.content)
        if not blocks:
            return {"url": url, "reason": reason}
        return {"url": url, "blocks": blocks, "text": "\n".join(f"[{page}] {text}" for page, text in blocks)}
    # primp decodes the charset (header or <meta>); extraction drops nav menus,
    # banners and footers. primp's own html->text is the fallback.
    html = _is_html(response)
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
    return {"url": url, "blocks": blocks, "text": text}

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

def fetch_all_blocks(urls: List[str]) -> Tuple[Dict[str, List[Tuple[str, str]]], List[Dict]]:
    """Fetch pages as blocks in parallel. Returns {url: blocks} plus the skipped pages."""
    pages, skipped = {}, []
    for url, (blocks, reason) in zip(urls, _parallel(fetch_blocks, urls, lambda u, r: ([], r))):
        if blocks:
            pages[url] = blocks
        else:
            skipped.append({"url": url, "reason": reason})
    texts = drop_near_duplicates([{"url": u, "text": "\n".join(t for _, t in b)} for u, b in pages.items()], skipped)
    return {p["url"]: pages[p["url"]] for p in texts}, skipped

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
