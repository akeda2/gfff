#!/usr/bin/env bash

# For rust repos
# Fetch and pull if repo has updates, then build. Needs gfff (git fetch and forward). Exits 22 if no updates are found.

set -e

gfff && { git pull --ff-only && cargo build --release ;} || { echo "Already up to date!"; exit 22 ;}
