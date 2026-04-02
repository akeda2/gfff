#!/usr/bin/env bash

# Use watch and pueue to schedule repo checks and trigger builds
SLEEP=900
#set -e
#watch -cwt -n $SLEEP "pueue add -g rsb rsb"

pueue group add --parallel 1 rsb
pueue clear -g rsb

while true; do
    if gfff; then
        pueue add -g rsb rsb
    fi
    sleep $SLEEP
done