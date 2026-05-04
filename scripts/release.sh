#!/usr/bin/env bash
# release.sh — bump spatiamed-common version, tag, and propagate the new pin
# into every consumer's pyproject.toml / requirements.txt override.
#
# Usage:  scripts/release.sh 0.1.2
#         scripts/release.sh 0.1.2 --dry-run

set -euo pipefail

NEW_VERSION="${1:-}"
DRY_RUN="${2:-}"

if [[ -z "$NEW_VERSION" ]]; then
    echo "usage: $0 <new-version> [--dry-run]" >&2
    exit 1
fi

if [[ ! "$NEW_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "error: version must be semver (e.g. 0.1.2)" >&2
    exit 1
fi

ROOT_REPO="$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)"
MONOREPO="$(dirname "$ROOT_REPO")"

CONSUMERS=(
    "$MONOREPO/platform-api/pyproject.toml"
    "$MONOREPO/CareLoop/pyproject.toml"
    "$MONOREPO/QueueCare/server/pyproject.toml"
    "$MONOREPO/QueueCare/notification_service/requirements.txt"
)

echo "=== spatiamed-common release: $NEW_VERSION ==="
echo "Monorepo root: $MONOREPO"

run() { if [[ "$DRY_RUN" == "--dry-run" ]]; then echo "DRY: $*"; else eval "$@"; fi; }

# 1. Bump pyproject.toml in spatiamed-common
echo "--- bumping spatiamed-common/pyproject.toml ---"
run "sed -i.bak 's|^version = \".*\"|version = \"${NEW_VERSION}\"|' \"$ROOT_REPO/pyproject.toml\""
run "rm -f \"$ROOT_REPO/pyproject.toml.bak\""

# 2. Commit + tag
echo "--- committing + tagging ---"
run "git -C \"$ROOT_REPO\" add pyproject.toml"
run "git -C \"$ROOT_REPO\" commit -m \"release: v${NEW_VERSION}\""
run "git -C \"$ROOT_REPO\" tag \"v${NEW_VERSION}\""

# 3. Update every consumer's git-URL override pin
for cfg in "${CONSUMERS[@]}"; do
    if [[ ! -f "$cfg" ]]; then
        echo "skip (not found): $cfg"
        continue
    fi
    echo "--- updating $cfg ---"
    run "sed -i.bak -E 's|spatiamed-common\\.git@v[0-9]+\\.[0-9]+\\.[0-9]+|spatiamed-common.git@v${NEW_VERSION}|g' \"$cfg\""
    run "rm -f \"$cfg.bak\""
done

cat <<EOF

=== next steps ===
1. Push tag:        git -C "$ROOT_REPO" push --tags
2. In each consumer repo: review the diff, commit, push:
$(printf '   %s\n' "${CONSUMERS[@]}" | sed 's|/[^/]*$||' | sort -u)
EOF
