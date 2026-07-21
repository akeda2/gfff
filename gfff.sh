#!/usr/bin/env bash
# Fetch and check if there's updates
# ex: gfff && echo "Updates available!" || echo "No updates"

set -e

git fetch --quiet

local=$(git rev-parse HEAD)
remote=$(git rev-parse @{u})

if [ "$local" = "$remote" ]; then
    exit 1
fi

# Return success only when upstream is ahead of local HEAD.
if git merge-base --is-ancestor "$local" "@{u}"; then
    exit 0
fi

exit 1
