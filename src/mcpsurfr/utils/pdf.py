"""
PDF text as (heading, text) blocks, one per page, so PDFs go through the same
passage and budget pipeline as HTML. Text layer only: scanned PDFs would need
OCR, which is far too heavy to ship.
"""

import io, logging, re
from itertools import islice
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

# a long report or a book shouldn't stall a search; the first pages carry the abstract,
# summary and contents anyway
MAX_PAGES = 50
MAX_BYTES = 20_000_000

_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")


def pdf_blocks(data: bytes) -> Tuple[List[Tuple[str, str]], Optional[str]]:
    """Blocks headed "Page N", or ([], why) when there's no usable text."""
    if len(data) > MAX_BYTES:
        return [], f"PDF too large ({len(data) // 1_000_000} MB)"
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # plenty of PDFs are "encrypted" with an empty password just to block editing
            reader.decrypt("")
        blocks = []
        for n, page in enumerate(islice(reader.pages, MAX_PAGES), 1):
            text = page.extract_text() or ""
            # layout line breaks aren't paragraphs: rejoin hyphenated words, then flatten
            text = " ".join(_HYPHEN_BREAK.sub(r"\1\2", text).split())
            if text:
                blocks.append((f"Page {n}", text))
    except Exception as e:
        log.warning("Couldn't read PDF: %s", e)
        return [], f"unreadable PDF ({type(e).__name__})"
    if not blocks:
        return [], "scanned PDF (no text layer)"
    return blocks, None
