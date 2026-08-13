#!/bin/zsh
# ดับเบิลคลิกไฟล์นี้เพื่อเปิด Gimmick Shot Planner
# Double-click this file to start the planner.
cd "$(dirname "$0")"
exec python3 shot_planner.py serve
