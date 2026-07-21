#!/usr/bin/env bash
# Fetch and check if there's updates
# ex: gfff && echo "Updates available!" || echo "No updates"

set -e

git fetch --quiet

local=$(git rev-parse HEAD)
remote=$(git rev-parse @{u})

if [ "$local" != "$remote" ]; then
    true
else
    false
fi
