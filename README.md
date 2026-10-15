# claude-camera

A Claude Code skill that lets Claude **see your workbench** through your iPhone (Continuity Camera)
or your Mac's camera, without you taking photos and pasting them in. Built for industrial designers,
product designers and makers.

Every image is cropped and downscaled before Claude looks at it (~260 tokens instead of ~1,600 for a
pasted phone photo), and long jobs run in the background and only report what matters.

## What you can ask Claude for

- "Take a look at the part on the desk": snapshot, cropped to the subject, with an optional grid
- "Tell me when the glue has set / the print has finished": a watcher that stays silent until something changes
- "Picture every 15 minutes for the next 6 hours": timelapse with an interval menu (5/10/15/30/45/60 min, or any duration)
- "Watch my 3D print": finds the print on the bed and shoots it when the print head is out of the way; follows the phone if it gets bumped. Optionally reads your printer's local API (Ultimaker, PrusaLink, OctoPrint) to log progress, and takes a final clear shot and stops when the job ends
- "Document this assembly": photo plus a numbered step in a `steps.md` build guide, per operation
- "Film a 20-second demo": an MP4 (60 fps or audio optional) plus a keyframe sheet Claude can review cheaply
- "Make a video of today's pictures": frames → MP4
- Sketch capture, CMF swatches, model iterations, fit checks, turntable product shots: see the recipes in `skills/camera/SKILL.md`

## Requirements

- macOS (Apple Silicon or Intel) with [Homebrew](https://brew.sh). `setup` installs ffmpeg for you.
- Claude Code (CLI, desktop app Code tab, or IDE extension)
- Recommended: an iPhone XR or newer on iOS 16+, signed in to the same Apple ID, with
  Settings > General > AirPlay & Continuity > **Continuity Camera** on. Without one, the Mac camera works.

## Install

In Claude Code:

```text
/plugin marketplace add mateodkaiser-bit/claude-camera
/plugin install claude-camera@claude-camera
```

Or copy `skills/camera/` to `~/.claude/skills/camera/` (no automatic updates that way).

## First run

Ask Claude: **"set up the camera skill"**. It runs `cam.py setup`, which checks everything, installs
what it can, and tells you the steps it can't do for you. Usually that's clicking **Allow** when macOS
asks "ClaudeCam would like to access the camera". `ClaudeCam` is a tiny helper app the skill builds in
`~/Applications`, because macOS won't give Claude Code itself camera access.

Then: mount the phone, lock it, put it on the charger, and ask Claude to "take a look".

## Privacy

Pictures and videos are saved locally only: in your project's `media/` folder, or in
`~/Pictures/claude-cam/`. They are sent to Claude only when Claude opens one to look at it, the same
as pasting a photo. Only record other people (user tests, co-design sessions) with their explicit consent.

## Limitations

- macOS only (AVFoundation and Continuity Camera). No Windows or Linux yet.
- No optical zoom through Continuity: close-ups are crops of the 1920×1440 frame.
- The 3D-print mode handles a dark print on a light bed, or a light print on a dark bed, as long as the
  print head is lighter than the bed. Tall prints on printers whose bed drops in Z drift down the frame.
- Printer API support was built from the vendors' documentation and is tested against mock servers;
  run `print printer-test` once with your printer to confirm.

## Updating (maintainers)

Edit the working copy in `~/.claude/skills/camera`, run `scripts/sync_from_local.sh`, run
`tests/run.sh` (regression tests on recorded fixtures, no camera or printer needed, ~3 min), bump
`version` in `.claude-plugin/plugin.json`, commit and push. Users get it with `/plugin marketplace update claude-camera`.

MIT licensed.
