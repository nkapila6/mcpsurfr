"""
Sites with a free, keyless API that beats their HTML page: Stack Exchange (the
page answers 403), Wikipedia (rate-limits page scrapes), GitHub (the README or
raw file reads cleaner than the page around it), Hacker News (the thread as one
JSON tree), and the PyPI, npm and crates.io registries (version, release date and
README without the page chrome). Each returns a doc shaped like fetch.fetch_doc's,
or None so the normal page fetch runs instead.

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


class _NotFound(Exception):
    """The API says the thing doesn't exist: a real answer, not a reason to scrape the page."""


def site_doc(url: str, client, timeout: int) -> Optional[Dict]:
    for pattern, handler in _SITES:
        m = pattern.match(url)
        if m:
            try:
                return handler(url, m, client, timeout)
            except _NotFound as e:
                return {"url": url, "reason": f"not found ({e})"}
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


def _json(client, url: str, timeout: int, gone: str = "", **kw):
    """GET and parse JSON. With `gone`, a 404 means the item doesn't exist, which is
    an answer: falling back to the page would only find an error or bot-check page."""
    r = client.get(url, timeout=timeout, **kw)
    if gone and r.status_code == 404:
        raise _NotFound(gone)
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
_MD_IMAGE = re.compile(r"\[?!\[[^\]]*\]\([^)]*\)\]?(?:\([^)]*\))?")
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
            # READMEs put badge images on the title line
            heading = " ".join(_MD_IMAGE.sub("", h.group(1)).split())
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


# --- Hacker News ----------------------------------------------------------------
# Algolia's HN API returns the whole comment tree in one call, no key

_HN = re.compile(r"https?://news\.ycombinator\.com/item\?id=(\d+)")
_HN_MAX_COMMENTS = 40


def _hackernews(url: str, m, client, timeout: int) -> Optional[Dict]:
    item = _json(client, f"https://hn.algolia.com/api/v1/items/{m.group(1)}", timeout, gone="Hacker News")
    title = item.get("title") or "Hacker News comment"
    head = [item["url"]] if item.get("url") else []
    if item.get("points") is not None:
        head.append(f"{item['points']} points by {item.get('author', '?')}")
    blocks = [(title, "\n".join(head + ([fragment_text(item["text"])] if item.get("text") else [])))]

    # thread order, top-level comments and their first replies, until the cap
    def walk(children, depth):
        for c in children:
            if len(blocks) > _HN_MAX_COMMENTS:
                return
            if c.get("text"):
                kind = "Comment" if depth == 0 else "Reply"
                blocks.append((f"{kind} by {c.get('author', '?')}", fragment_text(c["text"])))
            if depth < 1:
                walk(c.get("children") or [], depth + 1)

    walk(item.get("children") or [], 0)
    return _doc(url, blocks, title, (item.get("created_at") or "")[:10])


# --- package registries -----------------------------------------------------------
# what a coding agent wants first: latest version, when it shipped, the README

_PYPI = re.compile(r"https?://pypi\.org/project/([^/?#]+)/?(?:([^/?#]+)/?)?(?:[?#].*)?$")
_NPM = re.compile(r"https?://(?:www\.)?npmjs\.com/package/((?:@[^/]+/)?[^/?#]+)")
_CRATES = re.compile(r"https?://crates\.io/crates/([^/?#]+)(?:/([^/?#]+))?")
# crates.io asks API users for a User-Agent that says who they are
_CRATES_HEADERS = {"User-Agent": "mcpsurfr (https://github.com/nkapila6/mcpsurfr)"}


def _recent(versions: List[Tuple[str, str]], n: int = 10) -> str:
    """'1.2.0 (2026-10-01)' lines, newest first."""
    latest = sorted(versions, key=lambda v: v[1], reverse=True)[:n]
    return "\n".join(f"{v} ({d[:10]})" for v, d in latest)


def _readme_blocks(text: str, kind: str = "markdown") -> List[Tuple[str, str]]:
    if kind == "markdown":
        return _markdown_blocks(text)
    return [("Description", "\n".join(line for line in text.splitlines() if line.strip()))]


def _pypi(url: str, m, client, timeout: int) -> Optional[Dict]:
    name, version = m.group(1), m.group(2)
    data = _json(client, f"https://pypi.org/pypi/{name}/{version + '/' if version else ''}json", timeout, gone="PyPI")
    info = data["info"]
    released = (data.get("urls") or [{}])[0].get("upload_time_iso_8601", "")[:10]
    overview = [f"{info['name']} {info['version']}: {info.get('summary') or ''}".strip()]
    if released:
        overview.append(f"Released: {released}")
    if version:
        # search engines index old version pages; say what's current so they aren't quoted as latest
        current = _json(client, f"https://pypi.org/pypi/{name}/json", timeout)
        files = current.get("urls") or [{}]
        overview.append(f"Latest version: {current['info']['version']} ({files[0].get('upload_time_iso_8601', '')[:10]})")
        data["releases"] = current.get("releases")
    if info.get("requires_python"):
        overview.append(f"Requires Python: {info['requires_python']}")
    links = info.get("project_urls") or {}
    overview += [f"{k}: {v}" for k, v in links.items()]
    blocks = [("Overview", "\n".join(overview))]
    releases = [(v, files[0]["upload_time_iso_8601"]) for v, files in (data.get("releases") or {}).items() if files]
    if releases:
        blocks.append(("Recent releases", _recent(releases)))
    kind = "markdown" if "markdown" in (info.get("description_content_type") or "") else "text"
    blocks += _readme_blocks(info.get("description") or "", kind)
    return _doc(url, blocks, f"{info['name']} on PyPI", released)


def _npm(url: str, m, client, timeout: int) -> Optional[Dict]:
    name = m.group(1)
    data = _json(client, f"https://registry.npmjs.org/{name.replace('/', '%2F')}", timeout, gone="npm")
    latest = data.get("dist-tags", {}).get("latest", "")
    meta = data.get("versions", {}).get(latest, {})
    times = data.get("time", {})
    overview = [f"{name} {latest}: {data.get('description') or meta.get('description') or ''}".strip()]
    if times.get(latest):
        overview.append(f"Released: {times[latest][:10]}")
    repo = data.get("repository") or meta.get("repository") or {}
    for label, value in (("Homepage", data.get("homepage")), ("Repository", repo.get("url") if isinstance(repo, dict) else repo),
                         ("License", data.get("license") or meta.get("license"))):
        if value:
            overview.append(f"{label}: {value}")
    blocks = [("Overview", "\n".join(overview))]
    releases = [(v, t) for v, t in times.items() if v not in ("created", "modified")]
    if releases:
        blocks.append(("Recent releases", _recent(releases)))
    blocks += _readme_blocks(data.get("readme") or "")
    return _doc(url, blocks, f"{name} on npm", times.get(latest, "")[:10])


def _crates(url: str, m, client, timeout: int) -> Optional[Dict]:
    name = m.group(1)
    data = _json(client, f"https://crates.io/api/v1/crates/{name}", timeout, gone="crates.io", headers=_CRATES_HEADERS)
    crate = data["crate"]
    version = m.group(2) or crate.get("max_stable_version") or crate.get("max_version")
    overview = [f"{crate['name']} {version}: {crate.get('description') or ''}".strip(),
                f"Latest version: {crate.get('max_stable_version') or crate.get('max_version')}",
                f"Updated: {(crate.get('updated_at') or '')[:10]}",
                f"Downloads: {crate.get('downloads', 0):,}"]
    for label in ("repository", "documentation", "homepage"):
        if crate.get(label):
            overview.append(f"{label.capitalize()}: {crate[label]}")
    blocks = [("Overview", "\n".join(overview))]
    releases = [(v["num"], v["created_at"]) for v in data.get("versions") or [] if not v.get("yanked")]
    if releases:
        blocks.append(("Recent releases", _recent(releases)))
    r = client.get(f"https://crates.io/api/v1/crates/{name}/{version}/readme", timeout=timeout, headers=_CRATES_HEADERS)
    if r.status_code == 200 and r.text.strip():
        # the README arrives as rendered HTML; run it through the page extractor for its headings
        readme = extract_blocks(f"<html><body><main>{r.text}</main></body></html>")
        blocks += readme or [("README", fragment_text(r.text))]
    released = next((d for v, d in releases if v == version), "")[:10]
    return _doc(url, blocks, f"{crate['name']} on crates.io", released)


_SITES: List[Tuple[re.Pattern, Callable]] = [
    (_SE, _stackexchange),
    (_WIKI, _wikipedia),
    (_GH_BLOB, _github_blob),
    (_GH_REPO, _github_repo),
    (_HN, _hackernews),
    (_PYPI, _pypi),
    (_NPM, _npm),
    (_CRATES, _crates),
]
