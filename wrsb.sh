#!/usr/bin/env bash

# Use watch and pueue to schedule repo checks and trigger builds

set -e
#watch -cwt -n 2400 "pueue add -g rsb rsb"
while true; do
    pueue add -g rsb rsb
    sleep 2400
done