#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Main-content extraction: pull the article out of a page and leave the nav menus,
cookie banners and footers behind, so the 10k budget goes to actual content.
Uses lxml, which ddgs already depends on.
"""

import re
from typing import List, Optional, Tuple

from lxml import html as lxml_html
from lxml.etree import ParserError

_DROP = ("//script|//style|//noscript|//template|//svg|//canvas|//iframe|//form|//button"
         "|//select|//nav|//header|//footer|//aside|//dialog")
_DROP_ROLES = ('//*[@role="navigation" or @role="banner" or @role="contentinfo"'
               ' or @role="search" or @aria-hidden="true" or @hidden]')
# class/id words that mark page chrome rather than content
_CHROME = re.compile(
    r"(^|[\s_-])(nav|navbar|navigation|menu|masthead|footer|sidebar|breadcrumbs?|cookie|consent"
    r"|banner|social|share|sharing|related|comments?|advert|ads|promo|newsletter|subscribe"
    r"|skip|toolbar|popup|modal|toc|spinner|loader|skeleton)([\s_-]|$)", re.I)
_PERMALINKS = {"¶", "#", "§", "🔗"}
# below this a <main>/<article> is probably a teaser, not the page's content
_MIN_CONTENT = 500


def _text(el) -> str:
    # flat text for sizing and link density; itertext + join keeps blocks apart
    return " ".join(" ".join(el.itertext()).split())


# tags a browser breaks lines around. Inline tags (span, a, code) join without a
# space, so highlighted code doesn't come out as "tokio :: spawn ( async {"
_LINE_TAGS = {"address", "article", "blockquote", "dd", "details", "div", "dl", "dt", "fieldset",
              "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "li", "main", "ol",
              "p", "section", "summary", "table", "tbody", "thead", "tfoot", "ul", "caption"}
_PRE = "\x00"  # wraps preformatted runs so whitespace cleanup leaves them alone
_BR = "\x01"  # a real line break; newlines in the HTML source are just spaces


def _is_pre(el) -> bool:
    # some sites style <code> as a block instead of using <pre>
    return el.tag == "pre" or (el.tag == "code" and "\n" in "".join(el.itertext()).strip())


def _render(el) -> str:
    """Text the way a browser lays it out: one line per block, code blocks verbatim,
    one line per table row with cells split by " | "."""
    parts = []

    def walk(e, tail):
        tag = e.tag if isinstance(e.tag, str) else None
        if tag == "br":
            parts.append(_BR)
        elif tag and _is_pre(e):
            code = "".join(e.itertext()).strip("\n")
            parts.append(f"{_BR}{_PRE}{code}{_PRE}{_BR}" if code.strip() else _BR)
        elif tag == "tr":
            cells = [" ".join(_render(c).split()) for c in e if isinstance(c.tag, str) and c.tag in ("td", "th")]
            parts.append(_BR + " | ".join(c for c in cells if c) + _BR)
        elif tag:
            line = tag in _LINE_TAGS
            if line:
                parts.append(_BR)
            elif tag == "a" and parts and parts[-1][-1:].isalnum() and (e.text or "")[:1].isalnum():
                # tag clouds and link rows put <a>s back to back: "guide" "security"
                parts.append(" ")
            if e.text:
                parts.append(e.text)
            for child in e:
                walk(child, True)
            if line:
                parts.append(_BR)
        if tail and e.tail:
            parts.append(e.tail)

    try:
        walk(el, False)
    except RecursionError:
        return _text(el)
    out = []
    for i, chunk in enumerate("".join(parts).split(_PRE)):
        if i % 2:
            chunk = chunk.replace(_BR, "\n").replace("\xa0", " ")
            out.append("\n".join(line.rstrip() for line in chunk.split("\n")))
        else:
            out.extend(" ".join(line.split()) for line in chunk.split(_BR))
    return "\n".join(line for line in out if line.strip())


def extract_main_text(page: str) -> Optional[str]:
    root = _main_root(page)
    return None if root is None else (_render(root) or None)


# meta names sites use for the publish date, most specific first
_DATE_KEYS = ("article:published_time", "og:published_time", "datepublished", "publishdate", "pubdate",
              "dc.date.issued", "dc.date", "date", "parsely-pub-date", "sailthru.date")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def page_meta(page: str) -> dict:
    """Title and publish date (YYYY-MM-DD) from a page's metadata, where it has them."""
    try:
        doc = lxml_html.fromstring(page)
    except (ParserError, ValueError):
        return {}
    meta = {}
    title = doc.xpath('string(//meta[@property="og:title"]/@content)') or doc.xpath("string(//title)")
    if title.strip():
        meta["title"] = " ".join(title.split())[:200]

    found = {}
    for m in doc.xpath("//meta[@content]"):
        key = (m.get("property") or m.get("name") or m.get("itemprop") or "").lower()
        if key in _DATE_KEYS and key not in found:
            found[key] = m.get("content", "")
    dates = [found[k] for k in _DATE_KEYS if k in found]
    dates += _json_ld_dates(doc)
    # a <time> inside the article, not the one on a comment or the "updated" line in a sidebar
    dates += doc.xpath("//article//time[@datetime]/@datetime | //main//time[@datetime]/@datetime")[:1]
    for d in dates:
        iso = _ISO_DATE.match(d.strip())
        if iso:
            meta["date"] = iso.group(0)
            break
    return meta


_URL_DATE = re.compile(r"/(20\d\d|19\d\d)[/-](\d{1,2}|[A-Za-z]{3})[/-](\d{1,2})(?:/|$)")
_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def url_date(url: str) -> str:
    """Blogs that keep the date only in the path: /2024/Dec/31/ or /2024/12/31/."""
    m = _URL_DATE.search(url)
    if not m:
        return ""
    year, month, day = m.groups()
    month = int(month) if month.isdigit() else _MONTHS.get(month.lower(), 0)
    if not (1 <= month <= 12 and 1 <= int(day) <= 31):
        return ""
    return f"{year}-{month:02d}-{int(day):02d}"


def _json_ld_dates(doc) -> list:
    import json
    out = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("datePublished"), str):
                out.append(node["datePublished"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for script in doc.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            walk(json.loads(script))
        except ValueError:
            continue
    return out


def fragment_text(fragment: str) -> str:
    """Text of an HTML snippet, like a post body from an API, laid out the same way
    as a page: code blocks keep their lines."""
    try:
        return _render(lxml_html.fragment_fromstring(fragment, create_parent="div"))
    except (ParserError, ValueError):
        return ""


# a table row is one block, so its cells stay together
_BLOCKS = {"p", "li", "pre", "blockquote", "tr", "dd", "dt", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def extract_blocks(page: str) -> List[Tuple[str, str]]:
    """Main content as (nearest heading, text) blocks, in page order, for passage
    search. Only leaf blocks count, so a <li> holding a <p> isn't emitted twice."""
    root = _main_root(page)
    if root is None:
        return []
    blocks, heading = [], ""
    for el in root.iter(*_BLOCKS):
        if any(d.tag in _BLOCKS for d in el.iterdescendants()):
            continue
        text = _render(el) if el.tag in ("pre", "tr") else " ".join(_render(el).split())
        if not text:
            continue
        if el.tag in _HEADINGS:
            heading = text
        else:
            blocks.append((heading, text))
    # sites that put text straight into <div>s give few blocks; fall back to the flat text
    flat = _render(root)
    if sum(len(t) for _, t in blocks) < 0.3 * len(flat):
        return [("", line) for line in flat.split("\n")]
    return blocks


def _main_root(page: str):
    try:
        doc = lxml_html.fromstring(page)
    except (ParserError, ValueError):
        return None

    for el in doc.xpath(_DROP) + doc.xpath(_DROP_ROLES):
        if el.getparent() is not None:
            el.drop_tree()

    containers = doc.xpath('//main|//article|//*[@role="main"]')
    # nothing inside a heading is chrome: Sphinx wraps every section title in
    # <a class="toc-backref">, which the "toc" pattern would otherwise delete
    in_heading = set(doc.xpath("//h1//*|//h2//*|//h3//*|//h4//*|//h5//*|//h6//*"))
    for el in doc.xpath("//*[@class or @id]"):
        if el.tag in ("html", "body") or el in containers or el in in_heading:
            continue
        if _CHROME.search(f"{el.get('class', '')} {el.get('id', '')}") and el.getparent() is not None:
            el.drop_tree()

    for a in doc.xpath('//a[starts-with(@href, "#")]'):
        text = _text(a)
        # "skip to content" links, and the permalink marks Sphinx and MkDocs put
        # inside every heading, which otherwise end up as the passage's heading
        if (text.lower().startswith("skip") or text in _PERMALINKS) and a.getparent() is not None:
            a.drop_tree()
    _drop_link_lists(doc, containers)

    # a single real <article> is the content (a GitHub README inside <main>).
    # Many of them is a listing (a news front page), so use the whole <main> there.
    articles = [a for a in doc.xpath("//article") if len(_text(a)) >= _MIN_CONTENT]
    if len(articles) == 1:
        root = articles[0]
    else:
        sized = [(len(_text(c)), c) for c in doc.xpath('//main|//*[@role="main"]|//article')]
        sized = [(n, c) for n, c in sized if n >= _MIN_CONTENT]
        root = max(sized, key=lambda x: x[0])[1] if sized else doc.find("body")
        if root is None:
            root = doc
    return root


_LINKY = {"div", "ul", "ol", "section", "table", "dl"}


def _drop_link_lists(doc, keep) -> None:
    """Drop blocks that are mostly short links: breadcrumbs, nav menus, version
    pickers, terse tables of contents. Lists of descriptive links ("PEP 779: Free-
    threaded Python is officially supported") carry content and stay."""
    for el in list(doc.iter(*_LINKY)):
        if el in keep or el.getparent() is None or el.xpath(".//main|.//article"):
            continue
        total = len(_text(el))
        if total < 20:
            continue
        links = [_text(a) for a in el.iter("a")]
        linked = sum(len(t) for t in links)
        if len(links) < 2 or linked <= 0.5 * total or total - linked >= 200:
            continue
        # judge lists by their items: "PEP 649 and PEP 749: Deferred evaluation of
        # annotations" is several short links but one descriptive item
        units = [_text(li) for li in el.iter("li")] or links
        if sum(len(t.split()) for t in units) / len(units) < 4:
            el.drop_tree()
