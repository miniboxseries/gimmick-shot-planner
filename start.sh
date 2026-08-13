#!/bin/sh
# Linux / macOS terminal launcher
cd "$(dirname "$0")"
exec python3 shot_planner.py serve
