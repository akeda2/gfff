#!/usr/bin/env bash

# Use watch and pueue to schedule repo checks and trigger builds
SLEEP=3600
#set -e
#watch -cwt -n $SLEEP "pueue add -g rsb rsb"
while true; do
    pueue group add --parallel 1 rsb
    pueue add -g rsb rsb
    sleep $SLEEP
done