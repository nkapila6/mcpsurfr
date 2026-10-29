import asyncio, base64, logging, os, re
from pathlib import Path
from typing import List, Dict, Literal

from fastmcp import FastMCP, Context
from fastmcp.server.providers.skills import SkillsDirectoryProvider
from mcp.types import ElicitRequest, ElicitRequestFormParams, Icon, InputRequiredResult, ToolAnnotations

from mcpsurfr.utils.cache import TTLCache

_HERE = Path(__file__).parent

# ddgs text backends. "auto" is ddgs's metasearch: it queries several engines in
# parallel and moves on when one is blocked, so it's the dependable default.
BACKENDS = ("auto", "brave", "duckduckgo", "google", "grokipedia", "mojeek", "startpage", "wikipedia", "yahoo")
# wikipedia, grokipedia and mojeek ignore date limits, so "auto" with a recency filter
# sticks to the engines that honour it instead of mixing in undated results
DATED_AUTO = "brave,duckduckgo,google,startpage,yahoo"
# ddgs news engines. They all honour date limits.
NEWS_BACKENDS = ("auto", "bing", "duckduckgo", "yahoo")

Recency = Literal["day", "week", "month", "year"]
_RECENCY = {"day": "d", "week": "w", "month": "m", "year": "y"}
_REGION = re.compile(r"^[a-z]{2}-[a-z]{2}$")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

INSTRUCTIONS = """\
Local web search with on-device re-ranking, no API keys.

- search: use this first. Returns only the passages that answer the query (about a tenth of the
  tokens of full pages). Pass `keywords` with terms and synonyms a good answer would contain.
- rag_search_ddgs: full pages as text, for when the user wants whole pages. Takes a `backend`.
- read_url: one page you already have the link for (HTML or PDF). Pass a query to get only
  the passages that answer it.
- deep_research: broader topics. Give it 2-5 specific, related search_terms. Pass passages=true to
  get only the relevant passages per term instead of full pages.
- Every search tool takes recency ("day", "week", "month", "year"), region ("uk-en"),
  include_domains / exclude_domains, and news=true for news articles with dates.
- Google often blocks scrapers. When it returns nothing, the server retries with "auto" and says so.
- Results list pages that couldn't be used in `skipped`, with the search snippet for each in
  `snippets`, and engine problems in `warnings`.
- Cite the source URLs.

Full guide: the skill resource skill://mcpsurfr-search/SKILL.md"""

_ICON = Icon(
    src="data:image/svg+xml;base64," + base64.b64encode((_HERE / "icon.svg").read_bytes()).decode(),
    mime_type="image/svg+xml",
    sizes=["any"],
)
# these tools only read the web, so clients can treat them as safe to call
_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

def _version() -> str | None:
    # without this the server reports fastmcp's own version to clients
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("mcpsurfr")
    except PackageNotFoundError:
        return None

mcp = FastMCP("mcpsurfr", instructions=INSTRUCTIONS, version=_version(),
              website_url="https://github.com/nkapila6/mcpsurfr", icons=[_ICON])
# serves the bundled skill as resources, so clients don't have to install it by hand
mcp.add_provider(SkillsDirectoryProvider(roots=_HERE / "skills"))
log = logging.getLogger(__name__)

# rag_search_google and the deep_research_* presets are only backend= shortcuts. Every
# tool schema costs context in every chat, so they show up only for setups that name them.
LEGACY_TOOLS = os.environ.get("MCPSURFR_LEGACY_TOOLS", "").strip().lower() in ("1", "true", "yes")

def _legacy_tool(**kwargs):
    return mcp.tool(**kwargs) if LEGACY_TOOLS else (lambda fn: fn)

def _budget() -> int:
    """Characters of page text one call may return, shared across its pages. Set
    MCPSURFR_MAX_CHARS in the server's env to change it (~4 chars per token)."""
    raw = os.environ.get("MCPSURFR_MAX_CHARS", "").strip()
    try:
        return max(1000, int(raw)) if raw else 40000
    except ValueError:
        log.warning("MCPSURFR_MAX_CHARS=%r isn't a number, using 40000", raw)
        return 40000

MAX_CHARS = _budget()

def _deep_mode() -> bool:
    """True when deep_research should return passages unless a call says otherwise.
    MCPSURFR_DEEP_RESEARCH_MODE is "pages" (the default) or "passages"."""
    raw = os.environ.get("MCPSURFR_DEEP_RESEARCH_MODE", "").strip().lower()
    if raw not in ("", "pages", "passages"):
        log.warning("MCPSURFR_DEEP_RESEARCH_MODE=%r isn't pages or passages, using pages", raw)
    return raw == "passages"

DEEP_PASSAGES = _deep_mode()

def add_score_to_dict(query: str, results: List[Dict]) -> List[Dict]:
    """Add similarity scores to search results."""
    # Import heavy dependencies only when needed (slow import!)
    from importlib.resources import files
    from mcpsurfr.utils.fetch import fetch_embedder, get_path_str
    from mcpsurfr.utils.embedder import cosine_similarity

    path = get_path_str(files('mcpsurfr.embedder').joinpath('embedder.tflite'))
    embedder = fetch_embedder(path)
    query_embedding = embedder.embed(query)
    bodies = embedder.embed_batch([i['body'] for i in results])

    for i, body in zip(results, bodies):
        i['score'] = cosine_similarity(body, query_embedding)

    return results

# the search, ranking and fetching below are all blocking, so they run on threads
# to keep the server answering pings and progress while a search is in flight

# warnings go to the client log and into the result: most clients never show the
# log, and the model should know when an engine was blocked or a fallback ran
async def _warn(ctx: Context, notes: List[str], message: str) -> None:
    if message in notes:
        return
    notes.append(message)
    await ctx.warning(message)

def _extras(result: Dict, notes: List[str], skipped: List[Dict], results: List[Dict] | None = None) -> Dict:
    if skipped:
        result["skipped"] = skipped
        snippets = _snippets(skipped, results or [])
        if snippets:
            result["snippets"] = snippets
    if notes:
        result["warnings"] = notes
    return result

def _snippets(skipped: List[Dict], results: List[Dict]) -> List[Dict]:
    """The search engine's own snippet for each page that couldn't be read (blocked, rendered
    by JavaScript, behind a login like Reddit), so the model still gets something from it.
    Near-duplicates are left out: their text is already in the results."""
    by_url = {r["href"]: r for r in results if r.get("href")}
    out = []
    for page in skipped:
        found = by_url.get(page["url"])
        if found and found.get("body") and not (page.get("reason") or "").startswith("near-duplicate"):
            snippet = {"url": page["url"], "title": found.get("title", ""), "text": found["body"]}
            if found.get("date"):
                snippet["date"] = found["date"]
            out.append(snippet)
    return out

def _filters(recency: str | None = None, region: str | None = None, include_domains: List[str] | None = None,
             exclude_domains: List[str] | None = None, news: bool = False) -> Dict:
    """The search filters every tool takes, cleaned up. "problems" holds the ones that were dropped."""
    from mcpsurfr.utils.tools import clean_domains
    opts = {"include": clean_domains(include_domains), "exclude": clean_domains(exclude_domains), "problems": [],
            "news": news}
    if recency:
        opts["timelimit"] = _RECENCY[recency]
    region = (region or "").strip().lower()
    if _REGION.match(region):
        opts["region"] = region
    elif region:
        opts["problems"].append(f"Region {region!r} ignored, expected a code like us-en, uk-en, de-de or wt-wt")
    return opts

async def _start(ctx: Context, opts: Dict) -> List[str]:
    """A call's warning list, starting with any filters that were dropped."""
    notes = []
    for message in opts["problems"]:
        await _warn(ctx, notes, message)
    return notes

def _backends(opts: Dict) -> tuple:
    return NEWS_BACKENDS if opts.get("news") else BACKENDS

async def _check_backend(ctx: Context, notes: List[str], backend: str, opts: Dict) -> str:
    valid = _backends(opts)
    if backend in valid:
        return backend
    kind = "news " if opts.get("news") else ""
    await _warn(ctx, notes, f"Unknown {kind}backend {backend!r}, used auto. Available: {', '.join(valid)}")
    return "auto"

# raw engine results, keyed by everything that changes them. Only hits are kept, so a
# block or an empty answer is retried next time instead of being remembered.
_searches = TTLCache(maxsize=64)

async def _search(ctx: Context, notes: List[str], query: str, backend: str, max_results: int,
                  opts: Dict | None = None) -> List[Dict]:
    from ddgs import DDGS
    from mcpsurfr.utils.tools import domain_ok, site_query
    opts = opts or {}
    include, exclude = opts.get("include", []), opts.get("exclude", [])
    kwargs = {k: opts[k] for k in ("timelimit", "region") if k in opts}
    news = opts.get("news", False)
    engines = DATED_AUTO if backend == "auto" and "timelimit" in opts and not news else backend
    # off-domain results get dropped below, so ask for more up front
    wanted = max_results * 3 if include or exclude else max_results
    q = site_query(query, include)
    key = (q, wanted, engines, tuple(sorted(kwargs.items())))
    # news moves by the minute, so it always searches fresh
    cached = None if news else _searches.get(key)
    if cached is not None:
        # later steps write scores into the dicts, so hand out copies
        results = [dict(r) for r in cached]
    else:
        try:
            find = DDGS().news if news else DDGS().text
            results = await asyncio.to_thread(find, q, max_results=wanted, backend=engines, **kwargs)
        except Exception as e:
            # ddgs raises when an engine returns nothing, which is usually a block
            log.warning("Error searching %s for %r: %s", backend, query, e)
            await _warn(ctx, notes, f"{backend} returned no results for {query!r} ({e})")
            return []
        if results and not news:
            _searches.set(key, [dict(r) for r in results])
    for r in results:
        # news results call the link "url"
        r.setdefault("href", r.get("url"))
        # some news engines send "Opinion14 hours ago" instead of a date
        if r.get("date") and not _ISO_DATE.match(str(r["date"])):
            del r["date"]
    kept = [r for r in results if domain_ok(r.get("href") or "", include, exclude)]
    if results and not kept:
        await _warn(ctx, notes, f"{backend} found results for {query!r}, but none on the allowed domains")
    return kept[:max_results]

async def _fetch(ctx: Context, results: List[Dict], include_urls: bool):
    """Fetch pages best first within MAX_CHARS. Returns (content, skipped)."""
    from mcpsurfr.utils.fetch import fetch_all_content
    urls = [r["href"] for r in results if r.get("href")]
    content, skipped = await asyncio.to_thread(fetch_all_content, urls, MAX_CHARS)
    if skipped:
        await ctx.info(f"{len(skipped)} of {len(urls)} pages skipped (blocked, not a web page, empty or duplicate)")
    _add_meta(content, results)
    if not include_urls:
        for page in content:
            page.pop("url")
    return content, skipped

def _add_meta(items: List[Dict], results: List[Dict], meta: Dict[str, Dict] | None = None) -> None:
    """Give each page or passage its title and publish date. A news result's date beats the
    page's own metadata; the search result's title fills in when the page has none."""
    by_url = {r["href"]: r for r in results if r.get("href")}
    for item in items:
        url = item.get("url")
        found, page = by_url.get(url, {}), (meta or {}).get(url, {})
        title = item.get("title") or page.get("title") or found.get("title")
        date = found.get("date") or item.get("date") or page.get("date")
        if title:
            item["title"] = title
        if date:
            item["date"] = date

async def _rag_search(ctx: Context, query: str, backend: str, num_results: int, top_k: int, include_urls: bool,
                      opts: Dict | None = None) -> Dict:
    from mcpsurfr.utils.tools import sort_by_score

    opts = opts or _filters()
    notes = await _start(ctx, opts)
    backend = await _check_backend(ctx, notes, backend, opts)
    result = {"backend": backend}
    await ctx.report_progress(0, 3, f"Searching {backend} for {query!r}")
    results = await _search(ctx, notes, query, backend, num_results, opts)
    if not results and backend != "auto":
        await _warn(ctx, notes, f"Retried {query!r} with auto, since {backend} returned nothing")
        result.update(backend="auto", fallback=True)
        results = await _search(ctx, notes, query, "auto", num_results, opts)
    if not results:
        return _extras(dict(result, content=[]), notes, [])

    await ctx.report_progress(1, 3, f"Ranking {len(results)} results")
    scored_results = sort_by_score(await asyncio.to_thread(add_score_to_dict, query, results))
    top_results = scored_results[0:top_k]

    await ctx.report_progress(2, 3, f"Fetching {len(top_results)} pages")
    content, skipped = await _fetch(ctx, top_results, include_urls)
    await ctx.report_progress(3, 3, "Done")
    return _extras(result, notes, skipped, top_results) | {"content": content}

@mcp.tool(title="Web search", annotations=_READ_ONLY, icons=[_ICON])
async def rag_search_ddgs(ctx: Context, query: str, num_results:int=10, top_k:int=5, include_urls:bool=True,
                          backend: str = "auto", recency: Recency | None = None, region: str | None = None,
                          include_domains: List[str] | None = None, exclude_domains: List[str] | None = None,
                          news: bool = False) -> Dict:
    """
    Search the web and return the most relevant pages as text, for when whole pages are
    wanted (search returns only the relevant passages). Results are re-ranked locally by
    semantic similarity to the query before the top pages are fetched.

    Args:
        query (str): The search query. Use natural language questions or keywords.
                    Example: "latest developments in quantum computing"
        num_results (int): Number of initial search results to rank.
                          More results provide better coverage but increase processing time.
                          Default: 10
        top_k (int): Number of top-ranked pages to fetch and return.
                    Default: 5
        include_urls (bool): Whether to include each page's source URL for citation.
                            Default: True
        backend (str): Search backend, same names as deep_research. Default "auto" (ddgs
                       metasearch over several engines, skipping blocked ones). Non-auto
                       backends that return nothing are retried with "auto".
        recency (str | None): Only results from the past "day", "week", "month" or "year".
        region (str | None): Region-language code like "us-en", "uk-en" or "de-de". Default: ddgs's own (us-en).
        include_domains (List[str] | None): Only results from these sites, subdomains included.
            Example: ["python.org", "stackoverflow.com"]
        exclude_domains (List[str] | None): Never results from these sites. Example: ["pinterest.com"]
        news (bool): Search news articles instead of the web. Results then carry their published
            "date". News backends: auto, bing, duckduckgo, yahoo. Pair with recency for current events.

    Returns:
        Dict: {"backend", "content": [{"type": "text", "text", "url", "title", "date"}, ...]} ordered by
              relevance ("date" only when known),
              plus "fallback": true when the backend returned nothing and "auto" was used.
              All pages share one text budget (MCPSURFR_MAX_CHARS, default 40k chars); cut pages
              have "truncated": true and their full "chars". Also "skipped": [{"url", "reason"}]
              for pages left out, and "warnings" when an engine failed.
    """
    opts = _filters(recency, region, include_domains, exclude_domains, news)
    return await _rag_search(ctx, query, backend, num_results, top_k, include_urls, opts)

@_legacy_tool(title="Google search", annotations=_READ_ONLY, icons=[_ICON])
async def rag_search_google(ctx: Context, query: str, num_results:int=10, top_k:int=5, include_urls:bool=True) -> Dict:
    """
    Search Google and return the most relevant pages as text, re-ranked locally by
    semantic similarity. Google often blocks scrapers. When it returns nothing, this
    retries with ddgs "auto" metasearch and sends a warning saying so.

    Args:
        query (str): The query to search for.
        num_results (int): Number of initial search results to rank.
        top_k (int): Number of top-ranked pages to fetch and return.
        include_urls (bool): Whether to include each page's source URL.

    Returns:
        Same shape as rag_search_ddgs, with "backend": "auto" and "fallback": true when Google
        returned nothing.
    """
    return await _rag_search(ctx, query, "google", num_results, top_k, include_urls)

# last few search() results, so answering the "full pages?" question doesn't search
# and fetch again (on the 2026-07-28 protocol the tool re-runs with the answer)
_recent = TTLCache(maxsize=8)

def _can_ask(ctx: Context) -> bool:
    caps = getattr(ctx.session, "client_capabilities", None)
    return bool(caps and getattr(caps, "elicitation", None))

def _ask_full_pages(message: str) -> InputRequiredResult:
    params = ElicitRequestFormParams(
        message=message,
        requested_schema={
            "type": "object",
            "properties": {"full_pages": {"type": "boolean", "title": "Return full pages instead"}},
            "required": ["full_pages"],
        },
    )
    return InputRequiredResult(
        result_type="input_required",
        input_requests={"full_pages": ElicitRequest(method="elicitation/create", params=params)},
    )

@mcp.tool(title="Search (passages)", annotations=_READ_ONLY, icons=[_ICON])
async def search(
    ctx: Context,
    query: str,
    keywords: List[str] | None = None,
    num_results: int = 10,
    top_pages: int = 5,
    max_passages: int = 8,
    backend: str = "auto",
    recency: Recency | None = None,
    region: str | None = None,
    include_domains: List[str] | None = None,
    exclude_domains: List[str] | None = None,
    news: bool = False,
) -> Dict | InputRequiredResult:
    """
    Search the web and return only the passages that answer the query, not whole
    pages: usually about a tenth of the text of rag_search_ddgs. Use this first.

    Tiers, cheapest first:
    1. Search, then keep the top_pages results whose title/snippet match best.
    2. Fetch those pages, split them into ~600-char passages, keyword-match them (BM25)
       against the query plus `keywords`.
    3. Only if that leaves too many candidates, or matches nothing (a paraphrase),
       the local embedder ranks them by meaning.
    If nothing matched the keywords and the client can show questions, the user is
    asked whether they want the full pages instead.

    Args:
        query (str): The question or topic, in natural language.
        keywords (List[str] | None): Terms a good answer would contain, including
            synonyms and exact phrases. Example for "how do I make postgres faster":
            ["index", "EXPLAIN ANALYZE", "query planner", "vacuum"]. Strongly improves results.
        num_results (int): Search results to consider. Default 10.
        top_pages (int): Pages to fetch and split. Default 5.
        max_passages (int): Passages to return. Default 8.
        backend (str): Search backend, same names as deep_research. Default "auto".
        recency (str | None): Only results from the past "day", "week", "month" or "year".
        region (str | None): Region-language code like "us-en", "uk-en" or "de-de". Default: ddgs's own (us-en).
        include_domains (List[str] | None): Only results from these sites, subdomains included.
            Example: ["python.org", "stackoverflow.com"]
        exclude_domains (List[str] | None): Never results from these sites. Example: ["pinterest.com"]
        news (bool): Search news articles instead of the web. Results then carry their published
            "date". News backends: auto, bing, duckduckgo, yahoo. Pair with recency for current events.

    Returns:
        Dict with "passages": [{"url", "title", "date", "heading", "text", "matched"}, ...] best first
        ("date" only when the page or news result has one), and
        "tier" ("keywords", "keywords+semantic" or "semantic") saying what picked them.
        "matched" is the share of query and keyword terms in the passage (0-1), comparable
        across calls. Tier "semantic" means nothing matched: at most 4 passages and a "note".
        "skipped": [{"url", "reason"}] lists pages left out, with "snippets": [{"url", "title", "text"}]
        holding the search engine's snippet for each one it could; "warnings" any engine failures.
        If the user asked for full pages: "content": [{"type", "text", "url"}, ...] instead.
    """
    keywords = keywords or []
    key = (query, tuple(keywords), num_results, top_pages, max_passages, backend,
           recency, region, tuple(include_domains or []), tuple(exclude_domains or []), news)
    found = _recent.get(key) or await _find_passages(ctx, query, keywords, num_results, top_pages, max_passages,
                                                     backend, _filters(recency, region, include_domains, exclude_domains, news))
    # only remember answers worth repeating: a blocked or empty search should run again
    if found["pages"]:
        _recent.set(key, found)

    result = _extras({k: found[k] for k in ("query", "passages", "tier", "pages_fetched", "passages_considered")},
                     found["warnings"], found["skipped"], found["results"])
    low = ("Low confidence: no passage contains the query or keyword terms, so these few were picked by "
           "meaning alone and may be unrelated. Retry with different keywords or synonyms, rephrase the "
           "query, or use rag_search_ddgs for full pages.")
    if found["tier"] != "semantic" and found["keywords_missing"]:
        # common query words ("quantum", "cheese") matched, but none of the terms a real
        # answer would contain did: same doubt, same treatment as the semantic tier
        from mcpsurfr.utils.passages import SEMANTIC_MAX
        result["passages"] = result["passages"][:SEMANTIC_MAX]
        result["note"] = ("Low confidence: none of the keywords appear on the fetched pages, only common "
                          "query words. Retry with different keywords or synonyms, rephrase the query, or "
                          "use rag_search_ddgs for full pages.")
    if found["tier"] != "semantic" or not found["pages"]:
        return result

    # low confidence: no passage matched the keywords. Offer the old full-page dump.
    message = (f"No passage matched the keywords for {query!r}, so these were picked by "
               "meaning alone and may be off. Return the full pages instead? (about 10x more text)")
    if not _can_ask(ctx):
        result["note"] = low
        return result
    if ctx._is_modern_protocol():
        if ctx.input_responses is None:
            return _ask_full_pages(message)
        answer = ctx.input_responses.get("full_pages")
        wants_pages = bool(answer and answer.action == "accept" and answer.content.get("full_pages"))
    else:
        try:
            answer = await ctx.elicit(message, response_type=bool)
            wants_pages = answer.action == "accept" and bool(answer.data)
        except Exception as e:
            log.warning("Couldn't ask about full pages: %s", e)
            wants_pages = False
    if not wants_pages:
        return result
    from mcpsurfr.utils.fetch import fit_budget
    pages = [{"url": u, "text": "\n".join(t for _, t in blocks)} | found["meta"].get(u, {})
             for u, blocks in found["pages"].items()]
    return {"query": query, "content": fit_budget(pages, MAX_CHARS)}

async def _find_passages(ctx: Context, query: str, keywords: List[str], num_results: int,
                         top_pages: int, max_passages: int, backend: str, opts: Dict) -> Dict:
    from mcpsurfr.utils.fetch import fetch_all_blocks
    from mcpsurfr.utils.passages import keyword_scores, tokens

    notes = await _start(ctx, opts)
    empty = {"query": query, "passages": [], "tier": None, "pages_fetched": 0, "passages_considered": 0,
             "pages": {}, "meta": {}, "skipped": [], "results": [], "warnings": notes, "keywords_missing": False}
    backend = await _check_backend(ctx, notes, backend, opts)
    await ctx.report_progress(0, 4, f"Searching {backend} for {query!r}")
    results = await _search(ctx, notes, query, backend, num_results, opts)
    if not results and backend != "auto":
        await _warn(ctx, notes, f"Retried {query!r} with auto, since {backend} returned nothing")
        results = await _search(ctx, notes, query, "auto", num_results, opts)
    results = [r for r in results if r.get("href")]
    if not results:
        return empty

    # tier 1: pick pages by their search snippets, keyword hits first, then meaning
    await ctx.report_progress(1, 4, f"Picking {top_pages} of {len(results)} pages")
    results = await asyncio.to_thread(add_score_to_dict, query, results)
    snippets = [{"heading": r.get("title", ""), "text": r.get("body", "")} for r in results]
    hits = keyword_scores(query, keywords, snippets)
    ranked = sorted(zip(results, hits), key=lambda x: (x[1] > 0, x[1] + x[0]["score"]), reverse=True)
    urls = [r["href"] for r, _ in ranked[:top_pages]]

    await ctx.report_progress(2, 4, f"Fetching {len(urls)} pages")
    pages, skipped, meta = await asyncio.to_thread(fetch_all_blocks, urls)
    if skipped:
        await ctx.info(f"{len(skipped)} of {len(urls)} pages skipped (blocked, not a web page, empty or duplicate)")
    await ctx.report_progress(3, 4, "Scoring passages")
    chosen, tier, passages = await _pick_passages(query, keywords, urls, pages, max_passages)
    _add_meta(chosen, results, meta)
    await ctx.report_progress(4, 4, "Done")
    return {"query": query, "passages": chosen, "tier": tier, "pages_fetched": len(pages),
            "passages_considered": len(passages), "pages": {u: pages[u] for u in urls if u in pages}, "meta": meta,
            "skipped": skipped, "results": results, "warnings": notes,
            "keywords_missing": _keywords_missing(keywords, passages, tokens)}

async def _pick_passages(query: str, keywords: List[str], urls: List[str], pages: Dict, max_passages: int):
    """Tiers 2 and 3: split pages into passages, keywords first, the embedder only if it's
    needed. Returns (chosen, tier, every passage considered)."""
    from mcpsurfr.utils.passages import build_passages, keyword_scores, select
    passages, seen = [], set()
    for url in urls:
        for p in build_passages(url, pages.get(url, [])):
            # mirrors and syndicated copies repeat the same text across pages
            key = " ".join(p["text"].lower().split())[:200]
            if key not in seen:
                seen.add(key)
                passages.append(p)
    scores = keyword_scores(query, keywords, passages)
    embedder = await asyncio.to_thread(_embedder)
    chosen, tier = await asyncio.to_thread(select, query, keywords, passages, scores, max_passages, embedder.embed_batch)
    return chosen, tier, passages

def _keywords_missing(keywords: List[str], passages: List[Dict], tokens) -> bool:
    wanted = {t for k in keywords for t in tokens(k)}
    if not wanted:
        return False
    return not any(wanted & set(tokens(f"{p['heading']} {p['text']}")) for p in passages)

def _embedder():
    from importlib.resources import files
    from mcpsurfr.utils.fetch import fetch_embedder, get_path_str
    return fetch_embedder(get_path_str(files('mcpsurfr.embedder').joinpath('embedder.tflite')))

@mcp.tool(title="Read a page", annotations=_READ_ONLY, icons=[_ICON])
async def read_url(ctx: Context, url: str, query: str | None = None, keywords: List[str] | None = None,
                   max_passages: int = 8) -> Dict:
    """
    Read one page whose URL you already have: a link the user pasted, or one from earlier
    results. HTML or PDF. With a query, returns only the passages that answer it, the same
    way search does; without one, the page's main text (cut to the shared budget).

    Args:
        url (str): A public http(s) URL. Localhost and private network addresses are refused.
        query (str | None): What you want from the page. Leave out to get the whole text.
        keywords (List[str] | None): Terms a good answer would contain, as for search.
        max_passages (int): Passages to return when there's a query. Default 8.

    Returns:
        With a query: {"url", "passages": [{"url", "heading", "text", "matched"}], "tier"}, plus a
        "note" when nothing matched the terms. Without: {"url", "content": [{"type", "text", "url"}]},
        cut pages marked "truncated". {"url", "error"} when the page couldn't be read.
    """
    from mcpsurfr.utils.fetch import fetch_all_blocks, fetch_all_content
    from mcpsurfr.utils.tools import public_url

    if not public_url(url):
        return {"url": url, "error": "only public http(s) URLs can be read"}
    if not query:
        content, skipped = await asyncio.to_thread(fetch_all_content, [url], MAX_CHARS)
        if not content:
            return {"url": url, "error": skipped[0]["reason"]}
        return {"url": url, "content": content} | {k: content[0][k] for k in ("title", "date") if k in content[0]}

    keywords = keywords or []
    pages, skipped, meta = await asyncio.to_thread(fetch_all_blocks, [url])
    if not pages:
        return {"url": url, "error": skipped[0]["reason"]}
    chosen, tier, _ = await _pick_passages(query, keywords, [url], pages, max_passages)
    _add_meta(chosen, [], meta)
    result = {"url": url, "passages": chosen, "tier": tier} | meta.get(url, {})
    if tier == "semantic":
        result["note"] = ("Low confidence: no passage contains the query or keyword terms. Try other keywords, "
                          "or call read_url without a query for the whole text.")
    return result

async def _deep_research_internal(ctx: Context, search_terms:List[str], backends:List[str], num_results_per_term:int=5,top_k_per_term:int=3, include_urls:bool=True,
                                  opts: Dict | None = None, passages: bool | None = None, max_passages: int = 12)->Dict:
    """
    Internal function to perform deep research across multiple search term with the given backend engine in ddgs.

    Args:
        search_terms (List[str]): List of search terms to perform deep research on.
        backends (List[str]): List of search backends to use.
        num_results (int): Num of results to fetch per search term per engine.
        top_k (int): Number of top score to keep per search term per engine.
        include_urls (bool): whether to include urls in the results.

    Returns:
        Dict containing aggregated research results from all search terms and engines.
    """
    from mcpsurfr.utils.tools import sort_by_score

    opts = opts or _filters()
    notes, fallback = await _start(ctx, opts), None
    # ddgs would silently swap an unknown name for "auto", so say so instead
    valid = _backends(opts)
    unknown = [b for b in backends if b not in valid]
    if unknown:
        await _warn(ctx, notes, f"Unknown backends {unknown} skipped. Available: {', '.join(valid)}")
    backends = [b for b in backends if b in valid] or ["auto"]

    all_results = []
    search_summary = {term: {backend: 0 for backend in backends} for term in search_terms}
    step = len(search_terms) * len(backends)
    total = step + 1
    done = 0
    # each ddgs call already fans out to ~2 engines, so 3 at once is ~6 requests in flight
    sem = asyncio.Semaphore(3)

    async def ranked(term: str, backend: str) -> List[Dict]:
        nonlocal done
        async with sem:
            results = await _search(ctx, notes, term, backend, num_results_per_term, opts)
        top = sort_by_score(await asyncio.to_thread(add_score_to_dict, term, results))[:top_k_per_term] if results else []
        search_summary[term][backend] = len(top)
        done += 1
        await ctx.report_progress(min(done, step), total, f"Searched {backend} for {term!r}")
        return top

    # gather keeps task order, so results stay grouped by term like the old loop did
    async def search_all(pairs) -> None:
        for top in await asyncio.gather(*(ranked(t, b) for t, b in pairs)):
            all_results.extend(top)

    await ctx.report_progress(0, total, f"Searching {len(search_terms)} terms on {', '.join(backends)}")
    await search_all([(t, b) for t in search_terms for b in backends])

    # every chosen engine came back empty (Google blocking, usually): fall back to auto
    if not all_results and backends != ["auto"]:
        await _warn(ctx, notes, f"{', '.join(backends)} returned nothing, retried every term with auto")
        fallback = "auto"
        await search_all([(t, "auto") for t in search_terms])

    # remove duplicates and keep high scores
    seen_urls = {}
    unique_results = []
    for result in all_results:
        url = result.get('href', '')
        if url:
            # Keep the result with the highest score for duplicate URLs
            if url not in seen_urls or result.get('score', 0) > seen_urls[url].get('score', 0):
                if url in seen_urls:
                    # Replace lower scored duplicate
                    unique_results.remove(seen_urls[url])
                seen_urls[url] = result
                unique_results.append(result)

    result = {"search_terms": search_terms, "backends": backends}
    if fallback:
        result["fallback"] = fallback
    result.update(search_summary=search_summary, total_unique_results=len(unique_results))

    await ctx.report_progress(step, total, f"Fetching {len(unique_results)} pages")
    if DEEP_PASSAGES if passages is None else passages:
        from mcpsurfr.utils.fetch import fetch_all_blocks
        urls = [r["href"] for r in unique_results]
        pages, skipped, meta = await asyncio.to_thread(fetch_all_blocks, urls)
        picked, tiers = await _passages_per_term(search_terms, urls, pages, max_passages)
        _add_meta(picked, unique_results, meta)
        await ctx.report_progress(total, total, "Done")
        result.update(pages_fetched=len(pages), tiers=tiers)
        return _extras(result, notes, skipped, unique_results) | {"passages": picked}

    content, skipped = await _fetch(ctx, unique_results, include_urls)
    await ctx.report_progress(total, total, "Done")
    return _extras(result, notes, skipped, unique_results) | {"content": content}

async def _passages_per_term(terms: List[str], urls: List[str], pages: Dict, max_passages: int):
    """Rank passages for each term on its own, so every angle of the research gets covered,
    then interleave them best first. Returns (passages, {term: tier})."""
    per = max(2, -(-max_passages // max(1, len(terms))))
    picks, tiers = [], {}
    for term in terms:
        chosen, tiers[term], _ = await _pick_passages(term, [], urls, pages, per)
        picks.append([dict(p, term=term) for p in chosen])
    merged, seen = [], set()
    for rank in range(per):
        for chosen in picks:
            if rank < len(chosen):
                key = (chosen[rank]["url"], chosen[rank]["text"][:200])
                if key not in seen:
                    seen.add(key)
                    merged.append(chosen[rank])
    return merged[:max_passages], tiers

@mcp.tool(title="Deep research", annotations=_READ_ONLY, icons=[_ICON])
async def deep_research(
    ctx: Context,
    search_terms: List[str],
    backends: List[str] | None = None,
    num_results_per_term: int = 10,
    top_k_per_term: int = 3,
    include_urls: bool = True,
    recency: Recency | None = None,
    region: str | None = None,
    include_domains: List[str] | None = None,
    exclude_domains: List[str] | None = None,
    news: bool = False,
    passages: bool | None = None,
    max_passages: int = 12,
) -> Dict:
    """
    Research a topic across several search terms and engines. Each term is searched on
    each backend, results are re-ranked locally by relevance, duplicates are removed,
    and the best pages are fetched as text.

    Backends: auto, brave, duckduckgo, google, grokipedia, mojeek, startpage, wikipedia, yahoo.
    - "auto" (default): ddgs metasearch over several engines, skipping blocked ones. Most reliable.
    - "brave": fast and usually available.
    - "wikipedia": exact article lookups, best with title-like terms.
    - "google", "startpage", "yahoo", "mojeek": often blocked. Empty engines are reported
      as warnings, and if every chosen engine is empty the terms are retried with "auto".

    Args:
        search_terms (List[str]): 2-5 specific, related search queries covering different angles.
                                  Example: ["postgres query planner", "postgres index types", "EXPLAIN ANALYZE tutorial"]
        backends (List[str] | None): Backends to use. Defaults to ["auto"]. Unknown names are skipped with a warning.
        num_results_per_term (int): Number of results to rank per search term per backend.
        top_k_per_term (int): Number of top-ranked results to keep per search term per backend.
        include_urls (bool): Whether to include each page's source URL.
        recency (str | None): Applied to every term. Only results from the past "day", "week", "month" or "year".
        region (str | None): Region-language code like "us-en", "uk-en" or "de-de". Default: ddgs's own (us-en).
        include_domains (List[str] | None): Only results from these sites, subdomains included.
            Example: ["python.org", "stackoverflow.com"]
        exclude_domains (List[str] | None): Never results from these sites. Example: ["pinterest.com"]
        news (bool): Search news articles instead of the web. Results then carry their published
            "date". News backends: auto, bing, duckduckgo, yahoo. Pair with recency for current events.
        passages (bool | None): True returns only the passages that answer each term, about a
            tenth of the text of full pages: prefer it unless whole pages are needed. False returns
            full pages. Left out, the server's MCPSURFR_DEEP_RESEARCH_MODE decides (pages by default).
        max_passages (int): Passages to return in passages mode, shared across terms. Default 12.

    Returns:
        With passages: search_terms, backends, search_summary, "passages": [{"url", "title",
        "date", "heading", "text", "matched", "term"}, ...] (each term's best first, interleaved),
        "tiers" ({term: tier}, as in search), "skipped" and "warnings".
        Otherwise: Dict with search_terms, backends ("fallback": "auto" if they all came back empty),
        search_summary (results kept per term and backend), total_unique_results, "skipped"
        (pages left out, with reasons), "warnings", and content: [{"type": "text", "text", "url"}, ...].
        All pages share one text budget (MCPSURFR_MAX_CHARS, default 40k chars), so more
        terms means less text per page; cut pages have "truncated": true and their full "chars".
    """

    # safe default if none is given
    if backends is None:
        backends = ["auto"]

    return await _deep_research_internal(
        ctx,
        search_terms=search_terms,
        backends=backends,
        num_results_per_term=num_results_per_term,
        top_k_per_term=top_k_per_term,
        include_urls=include_urls,
        opts=_filters(recency, region, include_domains, exclude_domains, news),
        passages=passages,
        max_passages=max_passages,
    )


@_legacy_tool(title="Deep research (Google)", annotations=_READ_ONLY, icons=[_ICON])
async def deep_research_google(ctx: Context, search_terms: List[str], num_results_per_term:int=10, top_k_per_term:int=3, include_urls:bool=True) -> Dict:
    """
    Deep research using only Google: each term is searched, re-ranked locally, de-duplicated
    and the best pages fetched as text. Google often blocks scrapers. If it returns
    nothing, the terms are retried with ddgs "auto" metasearch and a warning is sent.

    Args:
        search_terms (List[str]): 2-5 specific, related search queries.
        num_results_per_term (int): Number of results to rank per search term.
        top_k_per_term (int): Number of top-ranked results to keep per search term.
        include_urls (bool): Whether to include each page's source URL.

    Returns:
        Same shape as deep_research.
    """
    return await _deep_research_internal(
        ctx,
        search_terms=search_terms,
        backends=["google"],
        num_results_per_term=num_results_per_term,
        top_k_per_term=top_k_per_term,
        include_urls=include_urls
    )


@_legacy_tool(title="Deep research (ddgs auto)", annotations=_READ_ONLY, icons=[_ICON])
async def deep_research_ddgs(ctx: Context, search_terms: List[str], num_results_per_term:int=10, top_k_per_term:int=3, include_urls:bool=True) -> Dict:
    """
    Deep research using ddgs "auto" metasearch, which queries several engines per term
    and skips blocked ones. Results are re-ranked locally, de-duplicated and the best
    pages fetched as text. Same as deep_research with its default backend.

    Args:
        search_terms (List[str]): 2-5 specific, related search queries.
        num_results_per_term (int): Number of results to rank per search term.
        top_k_per_term (int): Number of top-ranked results to keep per search term.
        include_urls (bool): Whether to include each page's source URL.

    Returns:
        Same shape as deep_research.
    """
    return await _deep_research_internal(
        ctx,
        search_terms=search_terms,
        backends=["auto"],
        num_results_per_term=num_results_per_term,
        top_k_per_term=top_k_per_term,
        include_urls=include_urls
    )


@mcp.prompt(title="Research a topic", icons=[_ICON])
def research(topic: str) -> str:
    """Research a topic with deep_research and write a cited summary."""
    return (
        f"Research this topic: {topic}\n\n"
        "1. Come up with 3-5 specific search terms that cover different angles of it.\n"
        "2. Call deep_research with those terms and passages=true (the default backend is fine).\n"
        "3. Write a concise summary. Cite the source URL for each claim, and point out "
        "where sources disagree or look out of date."
    )

if __name__ == "__main__":
    mcp.run()
