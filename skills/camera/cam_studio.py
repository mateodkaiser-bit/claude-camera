"""Setup and the "studio" commands: documentation steps, demo clips, compiling frames to video.

  cam.py setup                          first run on a new Mac: checks + fixes, then a test frame
  cam.py step "press bearing in" --project seedbomb-wheel [--note "..."] [--cam desk]
  cam.py clip --seconds 20 [--fps 60] [--audio] --project X --label demo
  cam.py compile <folder> [--match 'plate2-*.jpg'] [--fps 8]
"""
import json, platform, re, shutil, subprocess, sys, tempfile
from pathlib import Path

import cam as C

CONFIG = Path.home() / ".claude" / "camera-state" / "config.json"


def load_config():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- setup
def cmd_setup(args):
    """Walk a new install to a working camera. Fixes what it can; prints the exact human step otherwise.
    Claude: run this, relay every `YOU:` line to the user verbatim-ish, and re-run until READY."""
    todo, ok = [], lambda m: print(f"  ok   {m}")
    print("camera skill setup")
    if platform.system() != "Darwin":
        print("  FAIL this skill uses macOS AVFoundation + Continuity Camera; it does not run on "
              f"{platform.system()} yet.")
        return 1
    ok(f"macOS {platform.mac_ver()[0]}")
    if not Path(C.FFMPEG).exists():
        if shutil.which("brew"):
            print("  ...  installing ffmpeg with Homebrew (a few minutes)")
            subprocess.run(["brew", "install", "ffmpeg"])
        if not shutil.which("ffmpeg"):
            todo.append("Install ffmpeg: `brew install ffmpeg` (Homebrew: https://brew.sh), then re-run setup.")
            print("  FAIL ffmpeg missing")
            return _finish(todo)
        C.FFMPEG = shutil.which("ffmpeg")
    ok(f"ffmpeg {C.FFMPEG}")
    if not C.HELPER.exists():
        C.cmd_install_helper(args)
    ok(f"helper app {C.HELPER} (macOS camera permission is granted to this app)")
    cams = C.list_devices()
    phone = [d for d in cams if "iphone" in d.lower() and "desk view" not in d.lower()]
    print(f"  ...  cameras visible: {cams or 'none'}")
    if not phone:
        todo.append("For the iPhone camera (recommended): iPhone XR or newer on iOS 16+, signed in to the same "
                    "Apple ID as this Mac, Wi-Fi + Bluetooth on, Settings > General > AirPlay & Continuity > "
                    "Continuity Camera ON. Put it near the Mac, LOCKED, on a mount or propped up, on the charger.")
    print("  ...  test frame: a macOS prompt 'ClaudeCam would like to access the camera' may appear. Allow it.")
    args.cam = "phone" if phone else "mac"
    args.width, args.crop, args.grid, args.size = 320, None, None, None
    test = Path(tempfile.gettempdir()) / f"cam-setup-{C.ts()}.jpg"
    try:
        C.capture(args, test)
        ok(f"test frame from {args.cam} camera: {test}")
    except SystemExit as e:
        print("  FAIL no frames")
        todo.append("Allow the camera: System Settings > Privacy & Security > Camera > ClaudeCam ON. "
                    "If it is on and an iPhone is used: unlock the phone once, lock it, leave it still.")
        print("       " + str(e).replace("\n", "\n       "))
    cfg = load_config()
    cfg.update(default_cam=args.cam, cameras=cams, setup_at=C.ts())
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=1))
    ok(f"config saved: {CONFIG}")
    return _finish(todo)


def _finish(todo):
    if not todo:
        print("READY. Try: cam.py snap   (or ask Claude to 'take a look')")
        return 0
    print("NOT READY YET. Steps for the user:")
    for i, t in enumerate(todo, 1):
        print(f"  YOU: {i}. {t}")
    print("Then run `cam.py setup` again.")
    return 1


# ---------------------------------------------------------------- step (assembly / build documentation)
def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "step"


def cmd_step(args):
    """One documented step: a 1280 px photo + a numbered entry in steps.md (a ready assembly guide)."""
    out = C.outdir(args, "steps")
    md = out / "steps.md"
    n = len(list(out.glob("[0-9][0-9]-*.jpg"))) + 1
    img = out / f"{n:02d}-{slug(args.text)}.jpg"
    if "--width" not in sys.argv:
        args.width = 1280                       # documentation quality, not a token-cheap glance
    dev = C.capture(args, img, C.grid_filter(args.grid, args.width))
    if not md.exists():
        md.write_text(f"# Build steps\n\nPhotos taken with the camera skill. Newest at the bottom.\n")
    with md.open("a") as f:
        f.write(f"\n## {n}. {args.text}\n\n![step {n}]({img.name})\n\n"
                + (f"{args.note}\n\n" if args.note else "") + f"_{C.ts()} · {dev or 'file'}_\n")
    C.report(img, dev, f"\n  step {n} appended to {md}")


# ---------------------------------------------------------------- clip (demo video)
def cmd_clip(args):
    """Record a video (optionally with the camera's mic). Then a 3x2 keyframe sheet, so Claude can
    review the clip for ~600 tokens instead of watching it."""
    out = C.outdir(args, "clips")
    base = f"{args.label + '-' if args.label else ''}clip-{C.ts()}"
    mp4, sheet = out / f"{base}.mp4", out / f"{base}-keyframes.jpg"
    if args.input:
        src = ["-i", args.input]
        skip = 0
    else:
        dev = C.resolve(args.cam)
        spec = dev
        if args.audio:
            mics = C.list_devices("audio")
            stem = dev.split(" Camera")[0].split(" Desk View")[0]
            mic = next((m for m in mics if stem in m), mics[0] if mics else None)
            spec = f"{dev}:{mic}" if mic else dev
        size = args.size or ("1920x1080" if "iphone" in dev.lower() else None)
        src = ["-f", "avfoundation", "-pixel_format", "nv12", "-framerate", str(args.fps),
               *(["-video_size", size] if size else []), "-i", spec]
        skip = C.WARMUP_PHONE / 30 if "iphone" in dev.lower() else C.WARMUP_FRAMES / 30
    print(f"recording {C.human(args.seconds)}...", file=sys.stderr, flush=True)
    stderr = C.run_ffmpeg(["-hide_banner", "-loglevel", "error", "-y", *src, "-ss", f"{skip:.1f}",
                           "-t", f"{args.seconds:.1f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                           "-pix_fmt", "yuv420p", *(["-c:a", "aac", "-b:a", "128k"] if args.audio else ["-an"]),
                           "-movflags", "+faststart", str(mp4)],
                          camera=not args.input, timeout=args.seconds + skip + 40)
    if not mp4.exists() or mp4.stat().st_size == 0:
        sys.exit(f"CLIP FAILED: {stderr.strip()[-400:]}")
    dur = float(subprocess.run([C.FFMPEG.replace("ffmpeg", "ffprobe"), "-v", "error", "-show_entries",
                                "format=duration", "-of", "csv=p=0", str(mp4)],
                               capture_output=True, text=True).stdout.strip() or 0)
    # 6 evenly spaced keyframes -> one 3x2 sheet at 400 px per tile
    step = max(dur / 6, 0.1)
    C.run_ffmpeg(["-hide_banner", "-loglevel", "error", "-y", "-i", str(mp4), "-vf",
                  f"fps=1/{step:.3f},scale=400:-2,tile=3x2:padding=4:color=white", "-frames:v", "1",
                  "-q:v", "4", str(sheet)], camera=False)
    times = ", ".join(f"{i + 1}={step * i + step / 2:.1f}s" for i in range(6))
    print(f"{mp4}\n  {dur:.1f} s video, {mp4.stat().st_size / 1e6:.1f} MB"
          + (" with audio" if args.audio else "") + f"\n{sheet}\n  keyframes left->right, top->bottom: {times}")


# ---------------------------------------------------------------- compile frames -> MP4
def cmd_compile(args):
    folder = Path(args.folder).expanduser()
    frames = sorted(folder.glob(args.match))
    if len(frames) < 2:
        sys.exit(f"need at least 2 frames matching {args.match} in {folder}, found {len(frames)}")
    w, h = C.img_size(frames[0])
    lst = Path(tempfile.mkdtemp()) / "frames.txt"
    lst.write_text("".join(f"file '{f}'\nduration {1 / args.fps:.4f}\n" for f in frames)
                   + f"file '{frames[-1]}'\n")     # concat demuxer needs the last file twice
    out = Path(args.out).expanduser() if args.out else folder / f"{args.label or folder.name}-{C.ts()}.mp4"
    stderr = C.run_ffmpeg(["-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                           "-vf", f"scale={w // 2 * 2}:{h // 2 * 2}:force_original_aspect_ratio=decrease,"
                                  f"pad={w // 2 * 2}:{h // 2 * 2}:(ow-iw)/2:(oh-ih)/2:color=white,format=yuv420p",
                           "-r", str(args.fps), "-c:v", "libx264", "-crf", "20", str(out)],
                          camera=False, timeout=600)
    if not out.exists():
        sys.exit(f"COMPILE FAILED: {stderr[-400:]}")
    print(f"{out}\n  {len(frames)} frames at {args.fps} fps = {len(frames) / args.fps:.1f} s, "
          f"{out.stat().st_size / 1e6:.1f} MB")


COMMANDS = {"setup": cmd_setup, "step": cmd_step, "clip": cmd_clip, "compile": cmd_compile}
