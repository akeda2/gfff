#!/usr/bin/env bash

# For the yt-dlp repo
# Fetch and pull if repo has updates, then build. Needs gfff (git fetch and forward). Exits 22 if no updates are found.

set -e

function ytdlp_stuff () {
    git stash
    git pull --ff-only
    make clean
    make
    sudo make install
}
gfff && { ytdlp_stuff ;} || { echo "Already up to date!"; exit 22 ;}
