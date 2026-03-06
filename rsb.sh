#!/usr/bin/env bash

# For rust repos
# Fetch and pull if repo has updates, then build. Needs gfff (git fetch and forward).

set -e

gfff && { git pull --ff-only && cargo build --release ;} || { echo "Already up to date!"; false ;}
