# Skills

[`mcpsurfr-search/`](mcpsurfr-search/) is the skill for this server. It lives inside the package so the wheel and the Docker image carry it, and the server serves it as an MCP resource (`skill://mcpsurfr-search/SKILL.md`), so most clients need nothing extra.

Its `package.json` version is bumped by `create_tag.sh` along with the rest of the release. If the folder moves, update that script and the publish workflow too.
