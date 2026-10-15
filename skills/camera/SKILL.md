---
name: camera
description: Let Claude see the physical world through an iPhone (Continuity Camera) or the Mac camera, token-cheaply. Snapshots, before/after sheets, a watcher that stays silent until something changes, timed captures (5/10/15/30/45/60 min), 3D-print monitoring that finds the print and shoots when the print head is clear, build-step documentation, demo video clips, and timelapse videos. Use INSTEAD of asking the user for photos whenever a task depends on something physical - a print, a prototype, a fit test, a mechanism, a test rig, specimens, parts on the desk - and whenever they want progress pictures, assembly documentation, a demo video or a timelapse. If it isn't set up yet, run `setup` and walk the user through what it prints.
---

# Camera

`${CLAUDE_SKILL_DIR}/cam.py`: stdlib Python plus ffmpeg, macOS only. Every image is cropped and
downscaled **before** Claude sees it: a pasted phone photo costs ~1,600 tokens and stays in context,
while a 512 px snap costs ~260. Each command prints the saved path, size and token cost; `Read` the
path only when you actually need to look.

## First run (new user or new Mac)

If `~/.claude/camera-state/config.json` doesn't exist, or any capture fails, run:

```bash
python3 ${CLAUDE_SKILL_DIR}/cam.py setup
```

It checks macOS, installs ffmpeg through Homebrew if missing, builds the `ClaudeCam` helper app,
lists cameras, takes a test frame (which triggers the macOS camera prompt), and saves the config.
It prints `READY`, or numbered `YOU:` steps. **Relay those steps to the user in plain words, wait,
and re-run until READY.** Don't fall back to asking for photos without saying why.

What the user needs (setup explains it too): an iPhone XR or newer on iOS 16+ with the same Apple ID,
Wi-Fi and Bluetooth on, and Continuity Camera enabled; the phone locked, still and on the charger;
and **Allow** clicked on "ClaudeCam would like to access the camera". The Mac camera works as a
fallback (`--cam mac`).

## Pick the command for the job

| The user wants... | Command | Notes |
|---|---|---|
| A quick look | `snap [--crop x,y,w,h] [--grid 4x3]` | 512 px. Crop to the subject after the first look |
| To compare states | `sheet --n 4 --every 30s` | N frames tiled into ONE image |
| To be told when something happens | `watch --every 30s --until-change` | Silent until a change; names the region |
| Progress pictures over time | `timelapse --every 15m --for 6h` | Same framing every time |
| A 3D print watched | `print locate` → `print use <ids>` → `print monitor --every 5m --for 3h [--printer ultimaker --host IP]` | Shoots when the head is clear; with a printer API also logs progress/Z and stops with a final shot when the job ends |
| Assembly / build documented | `step "press bearing into hub" --project NAME [--note "..."]` | Photo plus a numbered entry in `steps.md` |
| A product demo video | `clip --seconds 20 [--fps 60] [--audio] --project NAME` | MP4 plus a keyframe sheet |
| A video from pictures | `compile <folder> [--match 'x-*.jpg'] [--fps 8]` | Timelapse, monitor or steps → MP4 |
| To check the setup | `doctor` / `setup` / `devices` | |

**Interval menu** for anything timed: `5m · 10m · 15m · 30m · 45m · 60m`, or any `90s` / `2h` / `48h`
(a bare number means seconds). Offer it with a sensible default: 5m for a print, 15–30m for curing,
glue or drying, 60m for slow processes over days.

**Long jobs** (`timelapse`, `watch`, `print monitor`): run them with `run_in_background`, as
`python3 -u ... | tee <out>/run.log`. They print one line per frame and keep going after a failed
frame. Don't Read every image: grep the log for `failed` / `WARNING` / `reconnect`, look when a line
looks off, and look at the end. `timelapse` and `print monitor` hold **one camera stream for the whole
job** (keep-alive) and reconnect it with backoff if it dies or stalls; add `--release` to free the
camera between frames instead (the phone is needed for something else, or the job is days long and the
battery matters more than reliability).

**Projects.** `--project NAME` finds `projects/NAME/` in the current directory or a parent. Running from
inside a project folder works without the flag. Output goes to `projects/NAME/media/{cam,steps,clips}/`.
Without a project, output goes to `~/Pictures/claude-cam/<date>/`, and `--out DIR` overrides both. After a
session of `step`s or a demo `clip`, offer to add a line to the project's log (if it keeps one) that
links the media.

## Recipes for design and prototyping work

Industrial design, product design and maker workflows, mapped onto the commands above. Always add
`--project NAME` when a project folder exists, so the media lands with the work.

| Moment in the process | Do this |
|---|---|
| **Sketch capture** (paper sketches, markers) | `snap --cam desk --width 1600 --label sketch-<topic>`. Desk View sees paper flat on the desk from above. Use one label per concept so iterations sort together |
| **Sketch or model iteration wall** | `compile` on a `--match 'sketch-*'` set at 1–2 fps, or `sheet`, to compare variants side by side |
| **Foam, clay or card model iterations** | `step "v3: narrowed grip 4 mm" --note "felt better, thumb rest too low"`, one step per iteration. The `steps.md` becomes the design-rationale trail |
| **CMF swatches** (colour, material, finish) | Desk View, even daylight, a white or grey card in frame; `snap --width 1280 --label cmf-<name>`. Same lighting every time, or colours can't be compared |
| **Fit and tolerance checks** | `snap --crop ... --width 1024` with a ruler or calipers in frame. Write the numbers into the project's tolerance notes, not just a photo |
| **Ergonomics: hand-hold and in-use photos** | `sheet --n 6 --every 3s` while the user changes grips: one image with every pose |
| **Turntable product shot** | Phone on a mount, the product on a lazy susan; `sheet --n 8 --every 4s` while the user turns it 45° per beep. For a clip instead: `clip --seconds 20 --fps 60` with one slow turn |
| **Mechanism or demo video** | `clip --seconds 15 --fps 60 [--audio]`; check the keyframe sheet; reshoot if framing is off. Use the MP4 for a Remotion or other edit |
| **Assembly or build guide** | `step` per operation in order (`--cam desk`); `steps.md` is the guide; `compile --fps 1` for a slideshow |
| **Glue, cure, dry, degrade** | `timelapse --every 15m --for 24h` (background); `compile` at the end |
| **Prints** | the 3D-print mode below |
| **User test or co-design session** | `clip --audio` **only with the participants' explicit consent**, and say what is recorded and where it is saved. Prefer framing hands and product over faces. Keep recordings inside the project folder, never in shared or public places |

## Set the shot up first

Before the first real capture, tell the user concretely how to place the phone **for this task**:
which camera, where to put the phone, distance, what must be in frame, and a scale reference.
- **Desk View** (`--cam desk`): the phone clipped on top of the Mac lid, rear camera facing the user.
  This gives a top-down view of the desk, which is ideal for `step` documentation of parts on the desk.
- **Main camera** (`--cam phone`): any mount or tripod aimed at the subject. For a printer, go higher
  and further back at ~45°, so the head doesn't hide the parts.
- Even light and no window in frame (auto-exposure drift reads as change). Use the same position every
  time (tape marks), and a **ruler or known object in frame** whenever size matters, because an unscaled
  image can't give millimetres.
- There's **no optical zoom** through Continuity: "zoom" means `--crop` on the 1920×1440 frame with a
  larger `--width`. Real detail needs the phone closer.

Then take one full snap, find the subject, and keep that `--crop` for the rest of the session. If a
number answers the question (a printer API, a measurement), skip the image. For repeated checks,
**delegate the looking** to a subagent ("Read <path>; answer <question> in one sentence"): its images
never enter the main context.

## Documentation and video (`cam_studio.py`)

- **step**: 1280 px photos (documentation quality, ~1,600 tokens if Read; don't Read them just to
  check them). Numbered `NN-slug.jpg` plus `steps.md` with headings, images and notes form a
  ready-to-share assembly guide. Use `--cam desk` for parts on the desk. Put fits, problems and
  tolerances in `--note`.
- **clip**: records with H.264 (CRF 20) and optional AAC from the same phone's mic. It skips the
  phone's ~5 s warm-up. iPhone modes: 1920×1080 or 1920×1440 at 30 or 60 fps. Afterwards, Read
  **only the keyframe sheet** (6 frames, ~600 tokens) to judge framing and action. The MP4 can go into a
  Remotion or other edit.
- **compile**: concat-demuxer slideshow at `--fps`; mixed frame sizes are letterboxed in white. 8 fps
  suits a timelapse; 1–2 fps suits a steps slideshow.

## 3D-print mode (`cam_print.py`)

1. **`print locate`** records ~40 s and builds a **25th-percentile composite** (per pixel, the darkest
   quarter of moments), so the print shows up even though the head covers it more than half the
   time. The median does NOT work: the head dwells over the part it's printing. It boxes dark-on-bed
   candidates ranked by how bright the bed around them is. The annotated 640×480 image costs ~410 tokens.
2. **`print use 1,2,4`**: **you decide which boxes are print.** Logo letters, belts, rail clips and rod
   shadows all pass the pixel tests, and the ranking has put rod shadows first before. If it's
   ambiguous, take one gridded `snap` and match cells. If nothing is print, ask the user to re-aim. The
   selection grows by `--grow` (default 0.15; use 0.08 when rod smears touch the parts) to pick up
   faint neighbouring parts.
3. **`print shot` / `print monitor`** stream at 2 fps and score each frame by **the share of print
   pixels visible right now**. They skip blurry frames and save a native-resolution crop. Mid-print,
   the head may never fully clear (60–75% visible is normal); for a clear plate, shoot after the print
   ends (or let the printer API do it, below).
   **Light filament on a dark bed** (white PLA on black PEI): `locate` checks the bed's brightness and,
   if it is dark, inverts the analysis so the print is the dark thing again; the locate image is then
   shown inverted and says `DARK BED`. Force it with `--invert on|off`. The 25th-percentile composite is
   taken *before* inverting: it removes the head because the head is lighter than what it covers, which
   holds for either print colour. A **dark head** on a light bed breaks that assumption (not built).
   **Large flat parts** (first-layer plates) fill the 25 px contrast window, so detection also accepts
   any pixel darker than 70. `locate` boxes up to **12** candidates: the 6 best "on the bed" plus the 6
   largest, because bed brightness falls off across the frame and on 15 Oct three plates in the darker
   bottom ranked 12th–18th and were never boxed. Plates merged with a **rod smear** (a thin moving line in
   the composite) are split: sparse blobs are eroded 3 px, which deletes anything under ~7 px wide, and
   each compact core is grown back inside its own outline.

**Printer API (optional, `--printer ultimaker|prusalink|octoprint --host <ip>`).** Numbers beat pixels
when you have them. One-time: `print printer-setup --printer prusalink --host <ip>` writes
`~/.claude/camera-state/printers.json` (mode 600) with empty fields; **the user fills in the password or
key in a text editor, never in the chat**. Ultimaker needs nothing (its local API reads without a
key). `print printer-test ...` shows what the printer answers. With it, `monitor`:
- logs `printer printing, 42%, Z 3.20 mm (~layer 16)` per shot (`--layer-height 0.2` for the layer
  number; OctoPrint gives state and progress only, no position);
- prefers frames where the head is ≥ 60 mm from its **dwell point** (the median of its track this job;
  the head dwells over the part it prints, the same fact behind the p25 composite) or a layer change is
  happening, and accepts them at 15% less visibility;
- checks the printer every 15 s between shots; when the job ends it waits for the head to park, takes a
  **final shot** (needs 90% visible) plus the full view (`-full.jpg`), logs `JOB FINISHED` and stops;
- prints `PRINTER ATTENTION` on paused/error (filament runout, a door open) and still shoots, so you
  can see why. An unreachable printer is logged and monitoring continues visually.
Endpoints (vendors' docs, 15 Oct 2026): Ultimaker `GET /api/v1/print_job`, `/api/v1/printer/heads/0/position`;
PrusaLink `GET /api/v1/status` (digest auth; older firmware `X-Api-Key`); OctoPrint `GET /api/job`
(`X-Api-Key`). Parsing is tested against mock servers built from those docs, **not yet against a real
printer**: on first use run `printer-test` and check the numbers look right.

**It adapts to a moved phone on its own,** conservatively. Each round compares its first 3 s with
the saved view using a *trimmed* match error (it ignores the 40% of pixels that differ most, wherever
the head is). It **follows a shift** only if the match improves by ≥ 4 and the shift is ≤ 60 px, because
a wrong shift is worse than none. A high error alone only marks the round *suspect*: **"CAMERA MOVED"
needs the print to be missing too** (< 15% visible). Then `monitor` re-runs `locate`, exits with
`NEEDS CONFIRM`, and the job ending notifies you; Read the locate image, run `print use`, and restart.
Calibration (15 Oct): recordings unmoved 3.2, bumped and aligned 4.6 (gain 4.3), different scene 12.0;
a live close-up head-heavy view, unmoved, scored 12.5 and offered a bogus +88,−64 shift (gain 3.7, rejected).
The first plain-mean version raised a false "moved" on the live close-up view, hence these guards.

## Reliability notes (verified 2026-10-15)

- **macOS permission goes to ClaudeCam.** Claude Code runs as its own helper bundle that declares no
  camera use, so macOS silently withholds the camera from anything it spawns (ffmpeg just hangs) and
  never lists it under Privacy > Camera. `cam.py` launches ffmpeg *through* `~/Applications/ClaudeCam.app`
  (built by `setup`/`install-helper`) with `open -W -g -n -a`; macOS asks once, for ClaudeCam.
- **"No frames" after it has worked before** is a stuck Continuity stream, not a permissions problem.
  Unlock the phone once, lock it, leave it still on the charger, and make sure no other app is using it.
  `devices` still listing the iPhone doesn't mean frames flow.
- **The phone needs ~5 s to connect and expose** (1.5 s gave pure black with no error), so it skips
  150 frames; the Mac camera skips 45. Device indices change between calls, so devices are picked by
  name. ffmpeg's default 29.97 fps is rejected, so it forces 30/60, nv12.
- **Keep-alive beats reconnecting.** Reconnecting the phone every round after minutes of idle gave
  "no frames" rounds and dropouts (15 Oct: rounds 2, 6 and 7 of 7 lost). `timelapse` and `print monitor` now hold
  one ffmpeg stream (`LiveStream` in cam.py): frames are deleted as they are read (disk stays flat), a
  stream that ends or delivers nothing for 25 s is killed and reconnected after 10, 20, 40 … 300 s,
  and the log says `stream stalled …; reconnecting`. The cap matters: on 15 Oct the iPhone dropped off
  the camera list for ~3 h (13:07–16:10) while the Mac slept on battery (14:12–16:10). A capped
  backoff keeps retrying every 5 min through that, so the job resumes within 5 min of the phone returning. Verified live 15 Oct (4 frames on a 30 s
  schedule over one connection, nothing left running after) and by tests (stall, end, drain).
- **Stalls** (with `--release`, or a single `shot`): a round with no frame for 25 s gives up and retries once. A stalled ffmpeg ignores
  SIGTERM (it once held the camera 9 min), so `ff_stop` escalates to SIGKILL, and each monitor round
  first runs `pkill -9 -f cam-shot-`. Killing `open` never stops the ffmpeg inside ClaudeCam.
- **Blur and freezes**: autofocus hunting once produced a fully blurred "best" frame. Shots skip frames
  under 60% of the round's sharpest and print `WARNING: BLURRY` / `WARNING: stream FROZE`.
- **The Mac must stay awake.** On 15 Oct the Mac slept ~20 min mid-monitor (lid closed, on battery)
  and every capture stopped, while the phone dropped off the camera list. `timelapse`, `watch` and
  `print monitor` now run `caffeinate -i -w <pid>` (no idle sleep while the job lives), print a NOTE when
  on battery, and log `round N is X late: the Mac was probably asleep`. Lid-closed sleep on battery
  can't be blocked: ask the user to keep the lid open and the charger in for long jobs.
- **Locked/asleep is required** (Continuity streams only from a locked phone). Waking it after idle is
  slower on battery or in Low Power Mode, so ask for the charger. A **moved** phone is the real risk:
  ask the user to check the view through `snap`, not by picking the phone up.
- **Regression tests**: `tests/run.sh` in the plugin package replays recorded fixtures (unmoved, bumped,
  re-aimed, blurred, large plates), synthetic light-on-dark and rod-smear scenes, keep-alive stalls, and
  mock Ultimaker/PrusaLink/OctoPrint servers (~3 min, no camera or printer). Run it after any change to
  the detection or streaming code; `CAM_DIR=~/.claude/skills/camera tests/run.sh` tests the working copy.
- **Known gap: printers whose bed drops in Z** (Ultimaker, Prusa CORE One): a tall print moves down
  the frame as it grows, which the fixed region doesn't follow. Fine for plates and short parts; for tall
  ones, re-run `locate` + `use` partway, or use `timelapse` with a crop.
- `open -W` always exits 0, so success means the output file exists. Homebrew ffmpeg has no `drawtext`,
  so timestamps and grid labels are printed as text. Use `--input <image|video>` to replay recordings
  for testing without a camera (state goes under `camera-state/file/`).
- Figma: `upload_assets` with `nodeIds` onto placeholder rectangles sized to the image's aspect ratio
  works for putting camera images into a frame.
