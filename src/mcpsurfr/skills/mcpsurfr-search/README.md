# mcpsurfr-search

A skill that tells the model how to use mcpsurfr: start with `search` and pass `keywords`, reach for `read_url` when there's a link, `deep_research` for broad topics, and use the filters (`recency`, `news`, domains) when they fit. It also covers how to read the results: `tier`, `matched`, `skipped`, `warnings`, and citing PDF pages.

The server serves it as `skill://mcpsurfr-search/SKILL.md`, so clients that read MCP resources pick it up without any setup. Install the server first: see the [main README](../../../../README.md#install).

To add it by hand:

- **Claude Code:** copy this folder to `~/.claude/skills/mcpsurfr-search/`.
- **Apps with a skills setting:** add this folder, or a zip of it.

The instructions themselves are in [SKILL.md](SKILL.md).
