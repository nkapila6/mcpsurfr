#!/bin/bash

# Cut a release: bump the version everywhere it's written down, commit, tag, push.
# The tag push triggers the publish workflow, which refuses tags that don't
# match these files, so always release through this script.
#
#   ./create_tag.sh 1.1.0          # release from main
#   ./create_tag.sh 1.1.0 --dry-run  # show what would change, touch nothing

set -euo pipefail

VERSION_FILES=(pyproject.toml src/mcpsurfr/skills/mcpsurfr-search/package.json server.json)

version="${1:-}"
dry_run="${2:-}"
latest=$(git describe --tags --abbrev=0 2>/dev/null || echo "none")

if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
	echo "usage: $0 X.Y.Z [--dry-run]   (latest tag: $latest)"
	exit 1
fi
tag="v$version"

if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
	echo "error: tag $tag already exists"
	exit 1
fi

if [[ "$dry_run" != "--dry-run" ]]; then
	if [[ "$(git rev-parse --abbrev-ref HEAD)" != "main" ]]; then
		echo "error: release from main"
		exit 1
	fi
	if ! git diff --quiet || ! git diff --cached --quiet; then
		echo "error: working tree has uncommitted changes"
		exit 1
	fi
	git pull --ff-only
fi

bump() {
	python3 - "$version" "$@" <<'EOF'
import json, re, sys
version, pyproject, package_json, server_json, write = *sys.argv[1:5], sys.argv[5] == "1"
changes = []

# only the [project] version line, not dependency pins
py = open(pyproject).read()
cur = re.search(r'(?m)^version = "([^"]*)"', py).group(1)
changes.append((pyproject, cur, re.sub(r'(?m)^version = "[^"]*"', f'version = "{version}"', py, count=1)))

# regex instead of json.dump so the files keep their formatting
js = open(package_json).read()
cur = json.loads(js)["version"]
new_js = re.sub(r'("version":\s*)"[^"]*"', rf'\g<1>"{version}"', js, count=1)
assert json.loads(new_js)["version"] == version
changes.append((package_json, cur, new_js))

# server.json has the version three times: the server's, the PyPI package's,
# and the image tag the registry points at
sv = open(server_json).read()
cur = json.loads(sv)["version"]
new_sv = re.sub(r'("version":\s*)"[^"]*"', rf'\g<1>"{version}"', sv)
new_sv = re.sub(r'("identifier":\s*"ghcr\.io/[^":]+:)v[^"]*"', rf'\g<1>v{version}"', new_sv)
d = json.loads(new_sv)
assert d["version"] == version
for pkg in d["packages"]:
    if pkg["registryType"] == "oci":
        assert pkg["identifier"].endswith(f":v{version}")
    else:
        assert pkg["version"] == version
changes.append((server_json, cur, new_sv))

for path, cur, new in changes:
    print(f"  {path}: {cur} -> {version}")
    if write:
        open(path, "w").write(new)
EOF
}

echo "Releasing $tag (latest tag: $latest)"
if [[ "$dry_run" == "--dry-run" ]]; then
	bump "${VERSION_FILES[@]}" 0
	echo "dry run: nothing written, committed or pushed"
	exit 0
fi

bump "${VERSION_FILES[@]}" 1
# uv.lock records the project's own version too
uv lock -q

git add "${VERSION_FILES[@]}" uv.lock
git commit -m "chore: release $tag"
git tag "$tag"
git push origin main "$tag"
echo "Pushed $tag. The publish workflow builds the image and creates the GitHub Release."
