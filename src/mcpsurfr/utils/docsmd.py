"""
Docs sites that publish markdown copies of their pages (llmstxt.org). The copy is
the page without its navigation, with headings, lists and code intact, so it
beats extracting the HTML.

Where to look: the page's own <link rel="alternate" type="text/markdown">, or
<page>.md on pages built with a docs generator that ships those copies. Ordinary
sites never get the extra request, and a host whose copies worked once skips its
HTML from then on.
"""

import logging, re, threading
from typing import Dict, Optional
from urllib.parse import urljoin, urlparse

from lxml import html as lxml_html
from lxml.etree import ParserError

from .adapters import _markdown_blocks

log = logging.getLogger(__name__)

# generators whose sites commonly serve <page>.md; a miss costs one request per host
_DOCS_GENERATORS = re.compile(r"mkdocs|mintlify|docusaurus|vitepress|astro|starlight|gitbook|fern|nextra|docsify|sphinx", re.I)
_MD_TYPES = ("text/markdown", "text/x-markdown", "text/plain")
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_TITLE_LINE = re.compile(r"^# \S", re.M)
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_FENCE_INFO = re.compile(r"^(\s*```[\w+-]*)[ \t].*$", re.M)

# host -> whether its pages have .md copies. Kept in memory for the server process,
# which is usually one client session; a new session relearns it for one extra request.
_hosts: Dict[str, bool] = {}
_lock = threading.Lock()


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def md_url(url: str) -> Optional[str]:
    """Where a page's markdown copy would be: /guide/ -> /guide/index.md, /guide -> /guide.md."""
    p = urlparse(url)
    path = p.path or "/"
    last = path.rsplit("/", 1)[-1]
    if path.endswith("/"):
        path += "index.md"
    elif last.endswith((".html", ".htm")):
        path = path.rsplit(".", 1)[0] + ".md"
    elif "." in last:
        return None  # a file (.pdf, .json, .md already), not a page
    else:
        path += ".md"
    return p._replace(path=path, query="", fragment="").geturl()


def known_host(url: str) -> bool:
    return _hosts.get(_host(url)) is True


def remember(url: str, ok: bool) -> None:
    with _lock:
        if ok:
            _hosts[_host(url)] = True
        else:
            # one miss on a host that worked before is just a page without a copy
            _hosts.setdefault(_host(url), False)


def from_html(page: str, url: str) -> Optional[str]:
    """The markdown copy a page points at, or the .md guess when a docs generator built it."""
    try:
        doc = lxml_html.fromstring(page)
    except (ParserError, ValueError):
        return None
    alt = doc.xpath('string(//link[@rel="alternate"][@type="text/markdown"]/@href)')
    if alt:
        return urljoin(url, alt)
    if _hosts.get(_host(url)) is False:
        return None
    generator = doc.xpath('string(//meta[@name="generator"]/@content)')
    return md_url(url) if _DOCS_GENERATORS.search(generator) else None


def fetch(client, md: str, url: str, timeout: int) -> Optional[Dict]:
    """The markdown copy as a doc like fetch.fetch_doc's, or None when there isn't one."""
    try:
        r = client.get(md, timeout=min(timeout, 5))
    except Exception as e:
        log.debug("No markdown copy at %s: %s", md, e)
        return None
    text = r.text if r.status_code == 200 else ""
    # catch-all servers answer any path with their HTML app shell
    if not r.headers.get("content-type", "").lower().startswith(_MD_TYPES) or text.lstrip()[:1] in ("<", ""):
        return None
    title = ""
    front = _FRONTMATTER.match(text)
    if front:
        text = text[front.end():]
        found = re.search(r"^title:\s*['\"]?(.+?)['\"]?\s*$", front.group(1), re.M)
        title = found.group(1) if found else ""
    text = _drop_preamble(text)
    # fences carry styling after the language (Mintlify: ```python theme={...}); keep the language
    text = _FENCE_INFO.sub(r"\1", text)
    # Mintlify opens every copy with a blockquote pointing at llms.txt
    blocks = [(_clean_heading(h), t) for h, t in _markdown_blocks(text) if not (t.startswith(">") and "llms.txt" in t)]
    if not blocks:
        return None
    title = title or next((h for h, _ in blocks if h), "")
    doc = {"url": url, "blocks": blocks, "text": "\n\n".join(f"{h}\n{t}" if h else t for h, t in blocks)}
    if title:
        doc["title"] = title
    return doc


def _drop_preamble(text: str) -> str:
    """Start at the page's own "# Title" when there is one. What some sites put before it
    isn't the page: llms.txt notices, and instructions addressed to the agent reading it
    (pydantic asks agents to report the user's intent in query parameters), which
    shouldn't reach the model as if they were documentation."""
    m = _TITLE_LINE.search(text)
    # only if the title isn't inside a code fence
    if m and text[:m.start()].count("```") % 2 == 0:
        return text[m.start():]
    return text


def _clean_heading(heading: str) -> str:
    """MkDocs headings come as "[Tools](#tools)": keep the words, drop links and images."""
    return " ".join(_MD_LINK.sub(r"\1", _MD_IMAGE.sub("", heading)).split())
