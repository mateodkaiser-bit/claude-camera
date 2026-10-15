#!/bin/sh
# Copy the working copy of the skill (~/.claude/skills/camera) into this repo before a release.
# Bump "version" in .claude-plugin/plugin.json afterwards, or users won't receive the update.
set -e
here="$(cd "$(dirname "$0")/.." && pwd)"
cp ~/.claude/skills/camera/SKILL.md ~/.claude/skills/camera/cam*.py "$here/skills/camera/"
echo "synced into $here/skills/camera — now bump the version in .claude-plugin/plugin.json"
