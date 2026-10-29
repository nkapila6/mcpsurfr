---
name: mcpsurfr-search
description: Search the web and research topics with the mcpsurfr MCP server, which needs no API keys and ranks results locally. Use it when you need current information, documentation, news, or several sources on a topic. Start with the `search` tool, which returns only the relevant passages.
---

# mcpsurfr search

mcpsurfr searches the web through ddgs, ranks the results on-device, and fetches the best pages. No API keys are involved. Its main tool, `search`, returns only the passages that answer the question, not whole pages: usually about a tenth of the tokens. Prefer it.

## Which tool

| Situation | Tool |
|---|---|
| Almost any question | `search` |
| The user wants whole pages, or `search` came back thin | `rag_search_ddgs` |
| A topic that needs several angles or sources | `deep_research` |
| The user asks for a specific engine | any of the above with `backend` (or `backends`) |

## Filters (every tool)

- `recency`: `day`, `week`, `month` or `year`. Use it for news, releases, prices, anything time-sensitive.
- `region`: a code like `uk-en` or `de-de` for local results.
- `include_domains` / `exclude_domains`: e.g. `["python.org"]` to stick to official docs, `["pinterest.com"]` to drop noise. Subdomains count.

## `search` (start here)

- `query`: the question in natural language
- `keywords`: **pass these.** Terms a good answer would contain, including synonyms and exact phrases, because the first ranking pass is keyword-based (BM25). For "how do I make postgres faster", pass `["index", "EXPLAIN ANALYZE", "query planner", "vacuum"]`.
- `max_passages` (8), `top_pages` (5), `num_results` (10), `backend` ("auto")

It returns `passages`: `[{url, heading, text, matched}]`, best first, plus `tier`, which says what picked them:
- `keywords` or `keywords+semantic`: confident.
- `semantic`: nothing matched your keywords, so at most 4 passages were picked by meaning only. Treat them with care.
- `matched` is the share of your query and keyword terms that the passage contains (0-1). It means the same in every call, so 0.1 is weak whatever else came back.
- A `note` means low confidence: either the `semantic` tier, or none of your `keywords` appear on any page. Retry with other keywords or synonyms, or use `rag_search_ddgs`. On `semantic`, the user may be asked whether they want full pages instead. If they say yes, you get `content` (full page text) rather than `passages`.

## `rag_search_ddgs`

Full page text (main content only, code blocks and tables kept) for the top `top_k` results. All pages share one budget, 40k characters by default. A page that didn't fit whole has `truncated: true` and its full length in `chars`. `backend` defaults to "auto", ddgs metasearch over several engines that skips blocked ones. Google often blocks scrapers. When it returns nothing, the server retries with "auto" and warns you.

## `deep_research`

- `search_terms`: 2-5 related queries, each covering a different angle (not just rewordings)
- `backends` (default `["auto"]`): `auto`, `brave`, `duckduckgo`, `google`, `grokipedia`, `mojeek`, `startpage`, `wikipedia`, `yahoo`
- `top_k_per_term` (3)

`search_summary` in the result shows which engines actually delivered, and `fallback: "auto"` means none of the chosen ones did. Full pages come back within the same shared budget, so more terms means less text per page. For one focused question, `search` is better.

Prompt: `research(topic)` plans the terms, runs `deep_research` and asks for a cited summary.

## Backends

`auto` (best: falls back across engines) and `brave` (fast, usually up) are dependable. `wikipedia` is an exact-title lookup. `duckduckgo` and `yahoo` get rate-limited at times. `google`, `startpage` and `mojeek` are often blocked. Empty engines, fallbacks and unknown names show up in the result's `warnings`.

## Skipped pages

Every tool lists pages it couldn't use in `skipped: [{url, reason}]`: `HTTP 403`, `timed out`, `not a web page`, `no readable content` (nav only or JavaScript-rendered), or `near-duplicate of <url>` (the same text at another address). Fewer pages than asked for is normal. If the one you needed is skipped, say so rather than guessing its content.

## Using results

1. Cite the `url` of everything you use.
2. Check dates, and say when a source looks stale.
3. Cross-check important facts across two sources.
4. Thin results: add keywords and synonyms, rephrase, raise `num_results`, or switch tools as in the table above.
