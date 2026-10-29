"""
Pages as markdown, and markdown cut into sections. Markdown keeps what flat text
loses: heading levels (so a section can include its subsections), links, lists,
tables and code fences. read_url uses it for outlines, single sections and the
markdown format.
"""

import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

from .extract import _is_pre, _main_root

_HEADINGS = {f"h{i}": i for i in range(1, 7)}
_BLOCKS = {"p", "div", "section", "article", "main", "header", "footer", "figure", "figcaption", "details",
           "summary", "dl", "dd", "dt", "address", "fieldset"}
_SKIP = {"img", "picture", "video", "audio", "source"}


def html_to_markdown(page: str, base_url: str) -> Optional[str]:
    """The page's main content as markdown, with the same chrome dropped as for plain text."""
    root = _main_root(page)
    if root is None:
        return None
    out: List[str] = []
    _walk(root, out, base_url, list_depth=0)
    md = "".join(out)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return md or None


def _inline(el, base_url: str) -> str:
    """An element's contents on one line. Walks the children, not the element itself,
    which would format it again (a link inside its own link)."""
    out: List[str] = []
    _walk_children(el, out, base_url, list_depth=0, inline=True)
    return " ".join("".join(out).split())


def _walk(el, out: List[str], base_url: str, list_depth: int, inline: bool = False) -> None:
    tag = el.tag if isinstance(el.tag, str) else None
    if tag in _SKIP:
        pass
    elif tag in _HEADINGS and not inline:
        text = _inline(el, base_url)
        if text:
            out.append(f"\n\n{'#' * _HEADINGS[tag]} {text}\n\n")
    elif tag and _is_pre(el) and not inline:
        code = "".join(el.itertext()).strip("\n")
        if code.strip():
            out.append(f"\n\n```\n{code}\n```\n\n")
    elif tag == "code":
        text = "".join(el.itertext())
        if text.strip():
            out.append(f"`{text.strip()}`")
    elif tag == "a" and el.get("href") and not el.get("href").startswith(("#", "javascript:")):
        text = _inline(el, base_url)
        if text:
            out.append(f"[{text}]({urljoin(base_url, el.get('href'))})")
    elif tag in ("strong", "b"):
        text = _inline(el, base_url)
        if text:
            out.append(f"**{text}**")
    elif tag in ("em", "i"):
        text = _inline(el, base_url)
        if text:
            out.append(f"*{text}*")
    elif tag == "br":
        out.append("\n")
    elif tag in ("ul", "ol") and not inline:
        out.append("\n")
        for n, li in enumerate((c for c in el if c.tag == "li"), 1):
            marker = f"{n}." if tag == "ol" else "-"
            item: List[str] = []
            _walk_children(li, item, base_url, list_depth + 1)
            text = "".join(item).strip()
            if text:
                # nested lists already came out indented one level deeper
                out.append(f"\n{'  ' * list_depth}{marker} {text}")
        out.append("\n\n")
    elif tag == "table" and not inline:
        out.append(_table(el, base_url))
    elif tag == "blockquote" and not inline:
        inner: List[str] = []
        _walk_children(el, inner, base_url, list_depth)
        text = re.sub(r"\n{3,}", "\n\n", "".join(inner)).strip()
        if text:
            out.append("\n\n" + "\n".join(f"> {line}" if line else ">" for line in text.split("\n")) + "\n\n")
    elif tag in _BLOCKS and not inline:
        out.append("\n\n")
        _walk_children(el, out, base_url, list_depth)
        out.append("\n\n")
    else:
        _walk_children(el, out, base_url, list_depth, inline)
    if el.tail and tag is not None:
        out.append(_space(el.tail))


def _walk_children(el, out: List[str], base_url: str, list_depth: int, inline: bool = False) -> None:
    if el.text:
        out.append(_space(el.text))
    for child in el:
        _walk(child, out, base_url, list_depth, inline)


def _space(text: str) -> str:
    # HTML source newlines are just spaces; markdown newlines come from the tags
    return re.sub(r"\s+", " ", text)


def _table(el, base_url: str) -> str:
    rows = []
    for tr in el.iter("tr"):
        cells = [_inline(c, base_url).replace("|", "\\|") for c in tr if isinstance(c.tag, str) and c.tag in ("td", "th")]
        if any(cells):
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n\n" + "\n".join(lines) + "\n\n"


def blocks_to_markdown(blocks: List[Tuple[str, str]], level: int = 2) -> str:
    """Markdown for docs built from (heading, text) blocks: PDF pages, API adapters."""
    out, current = [], None
    for heading, text in blocks:
        if heading and heading != current:
            out.append(f"{'#' * level} {heading}")
            current = heading
        # plain text from APIs and PDFs has "# comment" lines in code; they aren't headings
        out.append(_LEADING_HASH.sub(r"\\\1", text))
    return "\n\n".join(out)


# --- sections -----------------------------------------------------------------

_HEADING_LINE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_LEADING_HASH = re.compile(r"^(#)", re.M)
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def _clean(heading: str) -> str:
    # docs copies write headings as "[Tools](#tools)"; match and show the words
    return " ".join(_LINK.sub(r"\1", _IMAGE.sub("", heading)).split())


def outline(md: str) -> List[Dict]:
    """Headings in page order with their level and the size of their section (subsections included)."""
    return [{"heading": h, "level": lvl, "chars": end - start} for lvl, h, start, end in _sections(md)]


def section(md: str, name: str) -> Optional[str]:
    """The section under a heading, subsections included. Exact (case-insensitive) match first,
    then the first heading containing the name."""
    secs = _sections(md)
    want = name.strip().lower()
    match = next((s for s in secs if s[1].lower() == want), None) or \
        next((s for s in secs if want in s[1].lower()), None)
    return md[match[2]:match[3]].strip() if match else None


def _sections(md: str) -> List[Tuple[int, str, int, int]]:
    heads, pos, fence = [], 0, False
    for line in md.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        elif not fence:
            m = _HEADING_LINE.match(line)
            if m:
                heads.append((len(m.group(1)), _clean(m.group(2)), pos))
        pos += len(line) + 1
    out = []
    for i, (lvl, heading, start) in enumerate(heads):
        end = next((s for l, _, s in heads[i + 1:] if l <= lvl), len(md))
        out.append((lvl, heading, start, end))
    return out
