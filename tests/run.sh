#!/bin/sh
# Regression tests for the camera skill (no camera or printer needed). Usage: tests/run.sh [-k name]
# Test a working copy instead of this package: CAM_DIR=~/.claude/skills/camera tests/run.sh
cd "$(dirname "$0")/.." && exec python3 tests/run.py "$@"
