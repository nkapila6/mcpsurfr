"""
Sites with a free, keyless API that beats their HTML page: Stack Exchange (the
page answers 403), Wikipedia (rate-limits page scrapes) and GitHub (the README or
raw file reads cleaner than the page around it). Each returns a doc shaped like
fetch.fetch_doc's, or None so the normal page fetch runs instead.

Reddit was tried and left out: both its .json and old.reddit answer scrapers with
"blocked by network security" or a login page.
"""

import html, logging, re
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote

from .extract import extract_blocks, extract_main_text, fragment_text

log = logging.getLogger(__name__)

# Wikimedia asks API clients to say who they are
_WIKI_HEADERS = {"Api-User-Agent": "mcpsurfr (https://github.com/nkapila6/mcpsurfr)"}
# README or source file beyond this is a vendored blob, not something to read
_MAX_RAW = 500_000


def site_doc(url: str, client, timeout: int) -> Optional[Dict]:
    for pattern, handler in _SITES:
        m = pattern.match(url)
        if m:
            try:
                return handler(url, m, client, timeout)
            except Exception as e:
                log.info("Site API for %s failed, fetching the page instead: %s", url, e)
                return None
    return None


def _doc(url: str, blocks: List[Tuple[str, str]], title: str = "", date: str = "") -> Optional[Dict]:
    blocks = [(h, t) for h, t in blocks if t.strip()]
    if not blocks:
        return None
    doc = {"url": url, "blocks": blocks, "text": "\n\n".join(f"{h}\n{t}" if h else t for h, t in blocks)}
    if title:
        doc["title"] = title
    if date:
        doc["date"] = date
    return doc


def _json(client, url: str, timeout: int, **kw):
    r = client.get(url, timeout=timeout, **kw)
    r.raise_for_status()
    return r.json()


# --- Stack Exchange -----------------------------------------------------------
# 300 requests a day per IP without a key; each question costs two

_SE = re.compile(r"https?://(?:www\.)?(stackoverflow\.com|serverfault\.com|superuser\.com|askubuntu\.com"
                 r"|mathoverflow\.net|[a-z0-9-]+\.stackexchange\.com)/(?:questions|q)/(\d+)")


def _stackexchange(url: str, m, client, timeout: int) -> Optional[Dict]:
    # the API's site name is the first label: stackoverflow, superuser, unix (.stackexchange.com)...
    site, qid = m.group(1).split(".")[0], m.group(2)
    api = f"https://api.stackexchange.com/2.3/questions/{qid}"
    q = _json(client, f"{api}?site={site}&filter=withbody", timeout)["items"]
    if not q:
        return None
    q = q[0]
    answers = _json(client, f"{api}/answers?site={site}&filter=withbody&sort=votes&order=desc&pagesize=10",
                    timeout)["items"]
    # accepted first, then by votes: the order a reader scans the page in
    answers.sort(key=lambda a: (not a.get("is_accepted"), -a.get("score", 0)))
    title = html.unescape(q["title"])
    blocks = [(f"Question: {title}", fragment_text(q["body"]))]
    for a in answers:
        kind = "Accepted answer" if a.get("is_accepted") else "Answer"
        blocks.append((f"{kind} ({a.get('score', 0)} votes)", fragment_text(a["body"])))
    date = datetime.fromtimestamp(q["creation_date"], tz=timezone.utc).strftime("%Y-%m-%d")
    return _doc(url, blocks, title, date)


# --- Wikipedia ------------------------------------------------------------------

_WIKI = re.compile(r"https?://([a-z]{2,3}(?:-[a-z]+)*)\.(?:m\.)?wikipedia\.org/wiki/([^?#]+)")


def _wikipedia(url: str, m, client, timeout: int) -> Optional[Dict]:
    lang, page = m.group(1), m.group(2)
    if ":" in unquote(page):
        return None  # Special:, Talk:, File: pages have no article to read
    r = client.get(f"https://{lang}.wikipedia.org/api/rest_v1/page/html/{page}", timeout=timeout,
                   headers=_WIKI_HEADERS)
    r.raise_for_status()
    blocks = extract_blocks(r.text)
    if not blocks:
        text = extract_main_text(r.text) or ""
        blocks = [("", line) for line in text.split("\n")]
    return _doc(url, blocks, unquote(page).replace("_", " "))


# --- GitHub ---------------------------------------------------------------------

_GH_REPO = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?(?:[?#].*)?$")
_GH_BLOB = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+)/blob/([^?#]+)")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
# badge rows, logos and layout tags carry nothing worth reading
_MD_NOISE = re.compile(r"^\s*(?:<[^>]+>\s*)+$|^\s*(?:\[?!\[[^\]]*\]\([^)]*\)\]?(?:\([^)]*\))?\s*)+$")


def _markdown_blocks(md: str) -> List[Tuple[str, str]]:
    """Markdown as (heading, text) blocks: one per paragraph, code fences kept whole."""
    blocks, heading, buf, fence = [], "", [], False

    def flush():
        text = "\n".join(buf).strip()
        if text:
            blocks.append((heading, text))
        buf.clear()

    for line in md.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            buf.append(line)
            continue
        if fence:
            buf.append(line)
            continue
        h = _MD_HEADING.match(line)
        if h:
            flush()
            heading = h.group(1)
        elif not line.strip():
            flush()
        elif not _MD_NOISE.match(line):
            buf.append(line)
    flush()
    return blocks


def _github_repo(url: str, m, client, timeout: int) -> Optional[Dict]:
    owner, repo = m.group(1), m.group(2)
    r = client.get(f"https://api.github.com/repos/{owner}/{repo}/readme", timeout=timeout,
                   headers={"Accept": "application/vnd.github.raw"})
    r.raise_for_status()
    if len(r.content) > _MAX_RAW:
        return None
    return _doc(url, _markdown_blocks(r.text), f"{owner}/{repo}")


def _github_blob(url: str, m, client, timeout: int) -> Optional[Dict]:
    owner, repo, rest = m.group(1), m.group(2), m.group(3)
    r = client.get(f"https://raw.githubusercontent.com/{owner}/{repo}/{rest}", timeout=timeout)
    r.raise_for_status()
    if len(r.content) > _MAX_RAW or b"\x00" in r.content[:1024]:
        return None  # binary, or too big to be worth reading
    path = rest.split("/", 1)[-1]
    if path.lower().endswith((".md", ".markdown", ".mdx")):
        blocks = _markdown_blocks(r.text)
    else:
        # code: one block, its lines kept, so passages split on line ends
        blocks = [(path, r.text)]
    return _doc(url, blocks, f"{owner}/{repo}: {path}")


_SITES: List[Tuple[re.Pattern, Callable]] = [
    (_SE, _stackexchange),
    (_WIKI, _wikipedia),
    (_GH_BLOB, _github_blob),
    (_GH_REPO, _github_repo),
]
