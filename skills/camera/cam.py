#!/usr/bin/env python3
"""Token-cheap camera for Claude Code: iPhone Continuity Camera (or any Mac camera) via ffmpeg.

Stdlib only. Every image is cropped and downscaled BEFORE Claude sees it.

  cam.py doctor                       is everything working? (devices, permission, a real frame)
  cam.py devices                      list cameras
  cam.py snap  [--cam desk] [--crop x,y,w,h] [--width 512] [--grid 4x3]
  cam.py sheet --n 4 --every 20       N frames over time tiled into ONE image
  cam.py watch --every 30 [--until-change]   silent until the scene changes

--cam: phone (default) | desk (Desk View, top-down) | mac (built-in) | any device-name substring
--crop: fractions of the frame, e.g. 0.25,0.1,0.5,0.6 = x,y,w,h
--input FILE: process an image/video file instead of a camera (testing)
"""
import argparse, datetime as dt, os, re, shutil, signal, subprocess, sys, tempfile, time
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
DEFAULT_OUT = Path.home() / "Pictures" / "claude-cam"
# Claude Code runs as its own helper bundle (com.anthropic.claude-code) that declares no camera use,
# so macOS silently withholds the camera from anything it spawns and never lists it under
# Privacy > Camera. ClaudeCam.app declares camera use, so we launch ffmpeg *through* it with `open`:
# macOS then attributes the camera to ClaudeCam, asks once, and lists it.
HELPER = Path.home() / "Applications" / "ClaudeCam.app"
WARMUP_FRAMES = 45          # ~1.5 s at 30 fps: enough for the Mac camera
WARMUP_PHONE = 150          # ~5 s: Continuity Camera needs this to connect + expose (45 gave pure black)
CAPTURE_TIMEOUT = 40        # s; a silent hang almost always means no camera permission

PERMISSION_HELP = """\
CAMERA GAVE NO FRAMES (ffmpeg hung without an error). Two different causes:
  - It has NEVER worked on this Mac: macOS is withholding the camera. System Settings > Privacy &
    Security > Camera > switch ON "ClaudeCam" (run `cam.py doctor` and click Allow if it isn't listed;
    rebuild a missing helper with `cam.py install-helper`).
  - It worked before (most common): the iPhone's Continuity stream is stuck or busy. Unlock the phone
    once, lock it again, and leave it still on the charger. Make sure no other app (FaceTime, Camera,
    another call) is using it. Then retry. `cam.py devices` still listing the iPhone does NOT mean frames flow."""


INTERVALS = ["5m", "10m", "15m", "30m", "45m", "60m"]   # the standard menu for timed captures


def duration(s):
    """'5m' / '90s' / '1.5h' -> seconds. A bare number is seconds (keeps old commands working)."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", str(s).lower())
    if not m:
        raise argparse.ArgumentTypeError(f"'{s}' is not a duration; use e.g. {', '.join(INTERVALS)}, 90s, 2h")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def human(sec):
    return f"{sec / 3600:g} h" if sec >= 3600 and sec % 1800 == 0 else f"{round(sec / 60, 1):g} min" if sec >= 60 else f"{round(sec, 1):g} s"


def keep_awake(what):
    """Long jobs: stop the Mac idle-sleeping while this process lives (15 Oct: the Mac slept ~20 min
    mid-monitor and every capture stopped). Lid-closed sleep on battery can't be prevented: warn."""
    try:
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        batt = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True).stdout
        if "Battery Power" in batt:
            print(f"NOTE: the Mac is on battery. Keep the lid OPEN and plug it in for the whole {what}: "
                  "closing the lid on battery puts it to sleep and stops every capture.", flush=True)
    except OSError:
        pass


def sleep_gap(t_expected, every, k):
    """If this round started much later than scheduled, the Mac was asleep: say so in the log."""
    late = time.time() - t_expected
    if late > max(60, every * 0.5):
        print(f"[{ts()}] NOTE: round {k} is {human(late)} late: the Mac was probably asleep.", flush=True)


def ts():
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def list_devices(kind="video"):
    out = subprocess.run([FFMPEG, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                         capture_output=True, text=True).stderr
    video, audio, section = [], [], None
    for line in out.splitlines():
        if "video devices" in line:
            section = "v"
        elif "audio devices" in line:
            section = "a"
        else:
            m = re.search(r"\] \[(\d+)\] (.+)$", line)
            if m and section == "v" and not m.group(2).startswith("Capture screen"):
                video.append(m.group(2).strip())
            elif m and section == "a":
                audio.append(m.group(2).strip())
    return audio if kind == "audio" else video


def resolve(cam):
    devs = list_devices()
    c = cam.lower()
    if c == "phone":
        hits = [d for d in devs if "iphone" in d.lower() and "desk view" not in d.lower()]
    elif c == "desk":
        hits = [d for d in devs if "desk view" in d.lower()]
    elif c == "mac":
        hits = [d for d in devs if "iphone" not in d.lower() and "desk view" not in d.lower()]
    else:
        hits = [d for d in devs if c in d.lower()]
    if not hits:
        sys.exit(f"NO CAMERA matching '{cam}'. Cameras visible now: {devs or 'none'}.\n"
                 "iPhone missing? It must be near the Mac, locked, same Apple ID, Wi-Fi + Bluetooth on. "
                 "See SKILL.md setup.")
    return hits[0]


def crop_filter(crop):
    if not crop:
        return []
    x, y, w, h = (float(v) for v in crop.split(","))
    return [f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y}"]


def grid_filter(grid, width):
    if not grid:
        return []
    cols, rows = (int(v) for v in grid.lower().split("x"))
    t = max(2, width // 250)
    return [f"drawgrid=w=iw/{cols}:h=ih/{rows}:t={t}:c=yellow@0.8"]


def input_args(args, device=None):
    if args.input:
        return ["-i", args.input]
    if not args.size and "iphone" in device.lower() and "desk view" not in device.lower():
        args.size = "1920x1440"  # the phone's full 4:3 sensor mode (it also offers 640x480 .. 1920x1080)
    return ["-f", "avfoundation", "-pixel_format", "nv12", "-framerate", "30",
            *(["-video_size", args.size] if args.size else []), "-i", device]


def ff_start(ff_args, camera=True):
    """Start ffmpeg; through ClaudeCam.app when using a camera. Returns (proc, errlog, pidfile)."""
    err = tempfile.NamedTemporaryFile(prefix="cam-ff-", suffix=".log", delete=False).name
    if camera and HELPER.exists():
        pidf = err + ".pid"
        cmd = ["open", "-W", "-g", "-n", "-a", str(HELPER), "--stderr", err, "--args", pidf, *ff_args]
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), err, pidf
    with open(err, "w") as errf:
        return subprocess.Popen([FFMPEG, *ff_args], stdout=subprocess.DEVNULL, stderr=errf), err, None


def ff_stop(proc, pidf):
    """Stop ffmpeg. SIGTERM first; a capture STALLED inside AVFoundation ignores it (15 Oct: a stalled
    one kept the camera for 9 min and blocked the next shot), so escalate to SIGKILL after 3 s."""
    try:
        if pidf and Path(pidf).exists():
            pid = int(Path(pidf).read_text().strip())
            os.kill(pid, signal.SIGTERM)
            for _ in range(30):
                time.sleep(0.1)
                os.kill(pid, 0)                 # raises ProcessLookupError once it's gone
            os.kill(pid, signal.SIGKILL)
    except (ValueError, ProcessLookupError, PermissionError):
        pass
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()


def ff_err(err):
    try:
        return Path(err).read_text()
    except OSError:
        return ""


def run_ffmpeg(ff_args, camera=True, timeout=CAPTURE_TIMEOUT):
    proc, err, pidf = ff_start(ff_args, camera)
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        ff_stop(proc, pidf)
        sys.exit(PERMISSION_HELP)
    finally:
        out = ff_err(err)
        for f in (err, pidf):
            if f:
                Path(f).unlink(missing_ok=True)
    return out


class LiveStream:
    """ONE ffmpeg held open for a whole job (keep-alive), instead of reconnecting the phone every round.
    15 Oct: reconnecting after minutes of idle gave "no frames" rounds and dropouts; a stream that stays
    up keeps Continuity awake. Frames land as numbered files in a per-connection folder and are deleted
    as soon as they are read, so disk use stays flat however long the job runs.
    If the stream dies (ffmpeg exits) or stalls (no new frame), it reconnects with backoff
    (10 s, 20 s, 40 s ... capped at 5 min, reset by the next good frame), logging each attempt.

    outputs: [(filter chain, file pattern)], e.g. [("scale=320:240,format=gray", "s%06d.pgm"),
    ("null", "f%06d.jpg")]. The first pattern counts frames; the others are the same frame's siblings."""
    BACKOFF = [10, 20, 40, 80, 160, 300]

    def __init__(self, args, device, outputs, fps=2, tag="cam-live-", stall=25, log=None):
        self.args, self.device, self.outputs, self.fps, self.stall = args, device, outputs, fps, stall
        self.root = Path(tempfile.mkdtemp(prefix=tag))
        self.log = log or (lambda m: print(f"[{ts()}] {m}", flush=True))
        self.warm_s = 0 if args.input else (WARMUP_PHONE if "iphone" in device.lower() else WARMUP_FRAMES) / 30
        # replaying a file ends at EOF; only reconnect on files when a test asks for it
        self.reconnect = not args.input or bool(os.environ.get("CLAUDE_CAM_RECONNECT_FILES"))
        self.scale = float(os.environ.get("CLAUDE_CAM_BACKOFF", "1"))
        self.gen, self.fails, self.reconnects, self.proc = 0, 0, 0, None
        self.start()

    def start(self):
        self.gen += 1
        self.dir = self.root / f"c{self.gen}"
        self.dir.mkdir()
        n = len(self.outputs)
        head = f"[0:v]trim=start={self.warm_s},setpts=PTS-STARTPTS,fps={self.fps}"
        fc = head + (f",split={n}" + "".join(f"[o{k}]" for k in range(n)) + ";" if n > 1 else "[o0];")
        fc += ";".join(f"[o{k}]{chain}[p{k}]" for k, (chain, _) in enumerate(self.outputs))
        maps = [a for k, (_, pat) in enumerate(self.outputs)
                for a in ("-map", f"[p{k}]", *(["-q:v", "2"] if pat.endswith(".jpg") else []), str(self.dir / pat))]
        self.proc, self.errlog, self.pidf = ff_start(
            ["-hide_banner", "-loglevel", "error", "-y", *input_args(self.args, self.device),
             "-filter_complex", fc, *maps], camera=not self.args.input)
        self.t_start, self.t_frame, self.last, self.prev = time.time(), None, 0, []

    def stop(self):
        if self.proc:
            ff_stop(self.proc, self.pidf)
            self.proc = None

    def close(self):
        self.stop()
        shutil.rmtree(self.root, ignore_errors=True)
        for f in (self.errlog, self.pidf):
            if f:
                Path(f).unlink(missing_ok=True)

    def _siblings(self, f):
        num = re.search(r"(\d+)\.\w+$", f.name).group(1)
        return [self.dir / (re.sub(r"%0?\d*d", num, pat)) for _, pat in self.outputs[1:]]

    def _ready(self, ended):
        first = self.outputs[0][1]
        prefix = first.split("%")[0]
        fs = sorted(self.dir.glob(prefix + "*" + Path(first).suffix))
        fs = fs if ended else fs[:-1]            # while ffmpeg runs, the newest may be half-written
        return [f for f in fs if self._num(f) > self.last]

    @staticmethod
    def _num(f):
        return int(re.search(r"(\d+)\.\w+$", f.name).group(1))

    def _forget(self):
        for f in self.prev:
            f.unlink(missing_ok=True)
        self.prev = []

    def healthy(self, ended=None):
        """False when the stream died or stalled; reconnects (with backoff) if allowed. Returns True if
        frames should be flowing now. Pass `ended` as sampled BEFORE the last listing, or frames written
        between that listing and now would be skipped."""
        if ended is None:
            ended = self.proc is None or self.proc.poll() is not None
        quiet = time.time() - (self.t_frame or self.t_start)
        limit = self.stall + (0 if self.t_frame else self.warm_s + 6)
        if not ended and quiet < limit:
            return True
        if not self.reconnect:
            return False
        why = "ended" if ended else f"stalled (no frame for {quiet:.0f} s)"
        err = ff_err(self.errlog).strip()[-160:] if ended else ""
        self.stop()
        for f in (self.errlog, self.pidf):
            if f:
                Path(f).unlink(missing_ok=True)
        wait = self.BACKOFF[min(self.fails, len(self.BACKOFF) - 1)] * self.scale
        self.fails += 1
        self.reconnects += 1
        self.log(f"stream {why}{': ' + err if err else ''}; reconnecting in {human(wait)} "
                 f"(attempt {self.fails})")
        time.sleep(wait)
        # a stalled capture can linger inside ClaudeCam: clear this job's leftovers before reconnecting
        subprocess.run(["pkill", "-9", "-f", str(self.root)], capture_output=True)
        shutil.rmtree(self.dir, ignore_errors=True)
        self.start()
        return False

    def frames(self, deadline):
        """Yield (first-output path, [sibling paths]) for each new frame until the deadline. Survives
        reconnects. Returns early only if the stream is dead and may not reconnect."""
        while time.time() < deadline:
            ended = self.proc is None or self.proc.poll() is not None
            for f in self._ready(ended):
                self.last = self._num(f)
                sib = self._siblings(f)
                for _ in range(20):              # a sibling of the same frame may land a moment later
                    if all(s.exists() and s.stat().st_size > 0 for s in sib):
                        break
                    time.sleep(0.1)
                self._forget()
                self.prev = [f, *sib]
                self.t_frame, self.fails = time.time(), 0
                yield f, sib
                if time.time() >= deadline:
                    return
            if not self.healthy(ended):
                if not self.reconnect:
                    return
                continue
            time.sleep(0.25)

    def drain(self):
        """Between rounds: throw away frames nobody needs, keep the stream checked. Returns the newest
        complete frame's paths (first output, siblings) or None."""
        ended = self.proc is None or self.proc.poll() is not None
        fs = self._ready(ended)
        if fs:
            self.t_frame, self.fails = time.time(), 0
        self._forget()
        for f in fs[:-1]:
            for p in (f, *self._siblings(f)):
                p.unlink(missing_ok=True)
        if fs:
            self.last = self._num(fs[-1])
            self.prev = [fs[-1], *self._siblings(fs[-1])]
        newest = (fs[-1], self._siblings(fs[-1])) if fs else None
        if not self.healthy(ended) and self.reconnect:
            return None              # restarted: the old folder is gone
        return newest


def capture(args, out_path, extra_vf=(), warm=True):
    """One processed frame -> out_path. Retries once at a supported mode if the device rejects ours."""
    device = None if args.input else resolve(args.cam)
    warmup = WARMUP_PHONE if device and "iphone" in device.lower() else WARMUP_FRAMES
    vf = ([f"select=gte(n\\,{warmup})"] if warm and not args.input else []) \
        + crop_filter(args.crop) + [f"scale={args.width}:-2"] + list(extra_vf)
    Path(out_path).unlink(missing_ok=True)
    stderr = run_ffmpeg(["-hide_banner", "-loglevel", "error", "-y", *input_args(args, device),
                         "-vf", ",".join(vf), "-frames:v", "1", "-q:v", "3", str(out_path)],
                        camera=not args.input)
    if not Path(out_path).exists() and "Supported modes" in stderr and not args.size:
        modes = re.findall(r"(\d+)x(\d+)@\[[\d.]+ 30\.0+\]", stderr)
        if modes:
            w, h = max(modes, key=lambda m: int(m[0]) * int(m[1]))
            args.size = f"{w}x{h}"
            return capture(args, out_path, extra_vf, warm)
    if not Path(out_path).exists():
        sys.exit(f"CAPTURE FAILED on '{device or args.input}':\n{stderr.strip()[-800:]}")
    return device


def img_size(path):
    p = subprocess.run([FFMPEG.replace("ffmpeg", "ffprobe"), "-v", "error", "-show_entries",
                        "stream=width,height", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    w, h = (int(v) for v in p.stdout.strip().split(",")[:2])
    return w, h


def report(path, device, extra=""):
    w, h = img_size(path)
    print(f"{path}\n  {w}x{h} px, ~{w * h // 750} image tokens, camera: {device or 'file'}{extra}")


def project_dir(name):
    """Find a project folder: a path, or `projects/<name>` in the current directory or any parent
    (the ~/Prototyping layout), or the project you're standing in."""
    if not name:
        here = Path.cwd()
        for p in [here, *here.parents]:
            if p.parent.name == "projects" and (p / "log.md").exists():
                return p
        return None
    if Path(name).expanduser().is_dir():
        return Path(name).expanduser().resolve()
    for p in [Path.cwd(), *Path.cwd().parents]:
        if (p / "projects" / name).is_dir():
            return p / "projects" / name
    sys.exit(f"no project '{name}' found from {Path.cwd()} (looked for projects/{name} here and above)")


def outdir(args, sub=""):
    """--out wins; else <project>/media/<sub> when --project is given (or we're inside one); else
    ~/Pictures/claude-cam/<date>/<sub>."""
    if args.out:
        d = Path(args.out).expanduser()
    elif (proj := project_dir(getattr(args, "project", None))) is not None:
        d = proj / "media" / (sub or "cam")
    else:
        d = DEFAULT_OUT / dt.date.today().isoformat() / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


def grid_note(grid):
    if not grid:
        return ""
    cols, rows = (int(v) for v in grid.lower().split("x"))
    return (f"\n  grid {cols}x{rows}: cells A1 (top-left) .. "
            f"{chr(64 + cols)}{rows} (bottom-right); letters = columns left->right, numbers = rows top->bottom")


# ---------------------------------------------------------------- commands
HELPER_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>com.claude.camera-skill.helper</string>
  <key>CFBundleName</key><string>ClaudeCam</string>
  <key>CFBundleExecutable</key><string>ClaudeCam</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSUIElement</key><true/>
  <key>NSCameraUsageDescription</key><string>Lets Claude Code take small snapshots through your iPhone Continuity Camera or the Mac camera (camera skill).</string>
</dict></plist>
"""
HELPER_EXEC = f"""#!/bin/sh
# usage: ClaudeCam <pidfile> <ffmpeg args...>   (exec keeps the pid, so cam.py can stop it)
pidfile="$1"; shift
echo $$ > "$pidfile"
exec {FFMPEG} "$@"
"""


def cmd_install_helper(args):
    exe = HELPER / "Contents" / "MacOS" / "ClaudeCam"
    exe.parent.mkdir(parents=True, exist_ok=True)
    (HELPER / "Contents" / "Info.plist").write_text(HELPER_PLIST)
    exe.write_text(HELPER_EXEC)
    exe.chmod(0o755)
    subprocess.run(["codesign", "--force", "-s", "-", str(HELPER)], check=True)
    print(f"installed {HELPER} (ad-hoc signed). Next: cam.py doctor, and click Allow on the camera prompt.")


def cmd_devices(args):
    devs = list_devices()
    print("\n".join(devs) if devs else "no cameras visible")


def cmd_doctor(args):
    ok = True
    print(f"ffmpeg: {FFMPEG if Path(FFMPEG).exists() else 'MISSING (brew install ffmpeg)'}")
    if not HELPER.exists():
        cmd_install_helper(args)
    print(f"helper: {HELPER}")
    devs = list_devices()
    print(f"cameras: {devs or 'none'}")
    if not any("iphone" in d.lower() for d in devs):
        ok = False
        print("iPhone NOT visible: put it near the Mac, locked, same Apple ID, Wi-Fi + Bluetooth on.")
    print("test frame (up to 40 s; the phone may chime and show 'Connected to Mac')...")
    args.width, args.crop, args.grid, args.size = 320, None, None, None
    out = Path(tempfile.gettempdir()) / f"cam-doctor-{ts()}.jpg"
    try:
        dev = capture(args, out)
        report(out, dev)
        print("OK: camera works." if ok else "Frame OK, but see warnings above.")
        return 0 if ok else 1
    except SystemExit as e:
        print(e)
        return 1


def cmd_snap(args):
    path = outdir(args) / f"{args.label + '-' if args.label else ''}{ts()}.jpg"
    dev = capture(args, path, grid_filter(args.grid, args.width))
    report(path, dev, grid_note(args.grid))


def cmd_sheet(args):
    cols = args.cols or (2 if args.n <= 4 else 3)
    rows = -(-args.n // cols)
    tmp = Path(tempfile.mkdtemp(prefix="cam-sheet-"))
    stamps, dev = [], None
    for i in range(args.n):
        t0 = time.time()
        dev = capture(args, tmp / f"f{i:03d}.jpg")
        stamps.append(dt.datetime.now().strftime("%H:%M:%S"))
        print(f"  frame {i + 1}/{args.n} at {stamps[-1]}", file=sys.stderr, flush=True)
        if i < args.n - 1:
            time.sleep(max(0, args.every - (time.time() - t0)))
    path = outdir(args) / f"{args.label + '-' if args.label else ''}sheet-{ts()}.jpg"
    vf = f"tile={cols}x{rows}:padding=4:color=white" + ("," + grid_filter(args.grid, args.width * cols)[0]
                                                         if args.grid else "")
    p = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-framerate", "1",
                        "-i", str(tmp / "f%03d.jpg"), "-vf", vf, "-frames:v", "1", "-q:v", "3", str(path)],
                       capture_output=True, text=True)
    shutil.rmtree(tmp, ignore_errors=True)
    if p.returncode:
        sys.exit(p.stderr)
    order = ", ".join(f"{i + 1}={s}" for i, s in enumerate(stamps))
    report(path, dev, f"\n  {cols}x{rows} tiles, read left->right, top->bottom: {order}")


def cmd_timelapse(args):
    """A plain snap every `--every` for `--for`, same crop each time -> numbered frames that line up.
    For things that change slowly and are NOT a print on a bed (decay specimens, curing, a test rig).
    One line per frame; Claude reads only the ones that matter.
    Keep-alive (default): one stream for the whole run, a frame taken from it each tick (no 5 s phone
    warm-up per frame, no reconnect stalls). --release: reconnect per frame, camera free in between."""
    out = outdir(args)
    end, k, base = time.time() + args.for_, 0, args.label or "timelapse"
    print(f"timelapse every {human(args.every)} for {human(args.for_)} -> {out} "
          f"(camera {'released between frames' if args.release else 'kept connected'})", flush=True)
    keep_awake("timelapse")
    live = None
    if not args.release:
        device = None if args.input else resolve(args.cam)
        chain = ",".join(crop_filter(args.crop) + [f"scale={args.width}:-2"] + grid_filter(args.grid, args.width))
        # 1 frame every 2 s is plenty to pick from, and keeps the disk churn tiny
        live = LiveStream(args, device, [(chain, "t%06d.jpg")], fps=0.5, tag="cam-tl-")
    try:
        while time.time() < end:
            t0, k = time.time(), k + 1
            path = out / f"{base}-{k:03d}-{ts()}.jpg"
            if live:
                got = None
                for f, _ in live.frames(min(end, time.time() + 60)):   # the next fresh frame
                    got = f
                    break
                if got:
                    shutil.copy(got, path)
                    report(path, live.device)
                else:
                    print(f"[{ts()}] frame {k} failed: no frame within 60 s (stream reconnecting)", flush=True)
            else:
                try:
                    dev = capture(args, path, grid_filter(args.grid, args.width))
                    report(path, dev)
                except SystemExit as e:                 # camera lost: log and keep going
                    print(f"[{ts()}] frame {k} failed: {str(e)[:200]}", flush=True)
            sys.stdout.flush()
            while time.time() < min(end, t0 + args.every):
                if live:
                    live.drain()
                time.sleep(min(1, max(0, args.every - (time.time() - t0))))
    finally:
        if live:
            if live.reconnects:
                print(f"[{ts()}] stream reconnected {live.reconnects}x during the timelapse", flush=True)
            live.close()
    print(f"[{ts()}] timelapse finished after {k} frames", flush=True)


def read_pgm(path):
    data = Path(path).read_bytes()
    # P5\n<w> <h>\n<max>\n<bytes>
    parts = data.split(b"\n", 3)
    w, h = (int(v) for v in parts[1].split())
    return w, h, parts[3]


def diff_regions(a, b, w, h):
    """Mean abs diff overall and per 3x3 region (0-255)."""
    names = [["top-left", "top-centre", "top-right"], ["mid-left", "centre", "mid-right"],
             ["bottom-left", "bottom-centre", "bottom-right"]]
    tot, cells = 0, [[0] * 3 for _ in range(3)]
    for y in range(h):
        for x in range(w):
            d = abs(a[y * w + x] - b[y * w + x])
            tot += d
            cells[min(2, y * 3 // h)][min(2, x * 3 // w)] += d
    cell_px = (w * h) / 9
    worst = max(((cells[r][c] / cell_px, names[r][c]) for r in range(3) for c in range(3)))
    return tot / (w * h), worst


def cmd_watch(args):
    """One long-lived ffmpeg writes a tiny grey frame + a viewable JPEG every N s; we diff the grey ones.
    Prints ONLY on change, camera loss, or timeout, so it is cheap to run under Monitor/background."""
    device = None if args.input else resolve(args.cam)
    work = Path(tempfile.mkdtemp(prefix="cam-watch-"))
    keep = outdir(args)
    warm_s = 0 if args.input else (WARMUP_PHONE if "iphone" in device.lower() else WARMUP_FRAMES) / 30
    # drop the dark warm-up seconds, or the baseline is black and the first real frame is a "change"
    pre = ",".join([f"trim=start={warm_s},setpts=PTS-STARTPTS"] + crop_filter(args.crop) + [f"fps=1/{args.every}"])
    fc = (f"[0:v]{pre},split[a][b];[a]scale={args.width}:-2[big];"
          f"[b]scale=64:48,format=gray[small]")
    keep_awake("watch")
    proc, errlog, pidf = ff_start(["-hide_banner", "-loglevel", "error", "-y", *input_args(args, device),
                                   "-filter_complex", fc,
                                   "-map", "[big]", "-q:v", "3", str(work / "b%06d.jpg"),
                                   "-map", "[small]", str(work / "s%06d.pgm")], camera=not args.input)
    signal.signal(signal.SIGTERM, lambda *_: (ff_stop(proc, pidf), sys.exit(0)))
    print(f"watching '{device or args.input}' every {args.every}s, threshold {args.threshold} "
          f"(0-255 mean change); silent until something changes. frames kept in {keep}", flush=True)
    base, prev_alert, seen, start = None, None, 0, time.time()
    try:
        while True:
            ended = proc.poll() is not None
            frames = sorted(work.glob("s*.pgm"))
            # while ffmpeg runs, the newest file may be half-written: skip it until the next one lands
            for f in frames[seen:] if ended else frames[seen:-1]:
                seen += 1
                w, h, px = read_pgm(f)
                big = work / f.name.replace("s", "b", 1).replace(".pgm", ".jpg")
                if base is None:
                    base = px
                    shutil.copy(big, keep / f"watch-baseline-{ts()}.jpg")
                    print(f"[{ts()}] baseline captured", flush=True)
                    continue
                score, (cell, where) = diff_regions(base, px, w, h)
                if score >= args.threshold or cell >= args.threshold * 3:
                    if prev_alert is not None:  # confirmed on 2 consecutive frames: not an exposure flicker
                        out = keep / f"watch-change-{ts()}.jpg"
                        shutil.copy(big, out)
                        print(f"[{ts()}] CHANGE score {score:.1f} (strongest: {where}, {cell:.1f}) -> {out}",
                              flush=True)
                        if args.until_change:
                            return 0
                        base, prev_alert = px, None
                    else:
                        prev_alert = score
                else:
                    prev_alert = None
            if ended:
                err = ff_err(errlog).strip()
                print(f"[{ts()}] CAMERA LOST (stream ended). "
                      f"Phone picked up, moved away, or slept? {err[-300:]}", flush=True)
                return 2
            if args.max_min and time.time() - start > args.max_min * 60:
                print(f"[{ts()}] no change in {args.max_min} min; stopping.", flush=True)
                return 1
            time.sleep(1)
    finally:
        ff_stop(proc, pidf)
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cam", default="phone")
    common.add_argument("--crop")
    common.add_argument("--width", type=int, default=512)
    common.add_argument("--size", help="force capture mode, e.g. 1920x1440")
    common.add_argument("--out", help=f"output folder (default {DEFAULT_OUT}/<date>)")
    common.add_argument("--label", default="")
    common.add_argument("--grid", help="overlay grid, e.g. 4x3")
    common.add_argument("--input", help="file instead of a camera (testing)")
    common.add_argument("--project", help="save into <project>/media/... (name under projects/, or a path)")
    sub.add_parser("devices", parents=[common])
    sub.add_parser("install-helper", parents=[common])
    sub.add_parser("doctor", parents=[common])
    sub.add_parser("snap", parents=[common])
    s = sub.add_parser("sheet", parents=[common])
    s.add_argument("--n", type=int, default=4)
    s.add_argument("--every", type=duration, default=10, help="time between frames: 10s, 5m ...")
    s.add_argument("--cols", type=int)
    w = sub.add_parser("watch", parents=[common])
    w.add_argument("--every", type=duration, default=30, help="check interval: 30s, 5m ...")
    w.add_argument("--threshold", type=float, default=6)
    w.add_argument("--until-change", action="store_true")
    w.add_argument("--max-min", type=float, default=0, help="give up after N minutes (0 = never)")
    tl = sub.add_parser("timelapse", parents=[common], help="a snap every N for a duration")
    tl.add_argument("--every", type=duration, default=300, help=" / ".join(INTERVALS) + " (or any 90s / 2h)")
    tl.add_argument("--for", dest="for_", type=duration, default=7200, help="how long, e.g. 3h or 48h")
    tl.add_argument("--release", action="store_true",
                    help="free the camera between frames (reconnects each frame) instead of keep-alive")
    pr = sub.add_parser("print", parents=[common], help="find the print, shoot it when the head is clear")
    pr.add_argument("action", choices=["locate", "use", "shot", "monitor", "printer-setup", "printer-test"])
    pr.add_argument("ids", nargs="?", help="for `use`: candidate numbers from locate, e.g. 1,3")
    pr.add_argument("--seconds", type=float, default=40, help="locate: recording length")
    pr.add_argument("--grow", type=float, default=0.15, help="use: also take dark-on-bed pixels this far (frame fraction) around the picks")
    pr.add_argument("--max-wait", type=float, default=120, help="shot: give up after N s, keep the best frame")
    pr.add_argument("--every", type=duration, default=300, help="monitor: {} (or any 90s / 2h)".format(" / ".join(INTERVALS)))
    pr.add_argument("--max-min", type=float, default=150, help="monitor: stop after N minutes")
    pr.add_argument("--for", dest="for_", type=duration, help="monitor: how long, e.g. 3h (overrides --max-min)")
    pr.add_argument("--clear", type=float, default=0.85, help="shot: share of the print that must be visible")
    pr.add_argument("--invert", choices=["auto", "on", "off"], default="auto",
                    help="locate: light print on a dark bed (auto = decide from the bed's brightness)")
    pr.add_argument("--printer", choices=["ultimaker", "prusalink", "octoprint"],
                    help="monitor: also read the printer's API (state, progress, head position)")
    pr.add_argument("--host", help="printer IP or hostname, for --printer")
    pr.add_argument("--layer-height", type=float, help="monitor: turn Z into a layer number in the log")
    pr.add_argument("--release", action="store_true",
                    help="monitor: free the camera between shots (reconnects each round) instead of keep-alive")
    sub.add_parser("setup", parents=[common], help="first run on a new Mac: check + fix everything")
    st = sub.add_parser("step", parents=[common], help="assembly/build documentation: photo + numbered step")
    st.add_argument("text", help="what this step is, e.g. 'press bearing into hub'")
    st.add_argument("--note", default="", help="extra line under the step (tolerance, issue, ...)")
    cl = sub.add_parser("clip", parents=[common], help="record a demo video + keyframe sheet")
    cl.add_argument("--seconds", type=duration, default=15)
    cl.add_argument("--fps", type=int, default=30, choices=[30, 60])
    cl.add_argument("--audio", action="store_true", help="record the camera's microphone too")
    cp = sub.add_parser("compile", parents=[common], help="numbered frames -> MP4 (timelapse/monitor/steps)")
    cp.add_argument("folder")
    cp.add_argument("--match", default="*.jpg", help="which files, e.g. 'plate2-r2-*.jpg'")
    cp.add_argument("--fps", type=int, default=8)
    args = ap.parse_args()
    if args.cmd in ("setup", "step", "clip", "compile"):
        from cam_studio import COMMANDS
        sys.exit(COMMANDS[args.cmd](args) or 0)
    if args.cmd == "print":
        from cam_print import cmd_print
        if args.action in ("shot", "monitor") and "--width" not in sys.argv:
            args.width = 1024
        sys.exit(cmd_print(args) or 0)
    if args.cmd == "sheet":
        args.width = args.width if "--width" in sys.argv else 400
    sys.exit({"devices": cmd_devices, "timelapse": cmd_timelapse, "install-helper": cmd_install_helper, "doctor": cmd_doctor, "snap": cmd_snap,
              "sheet": cmd_sheet, "watch": cmd_watch}[args.cmd](args) or 0)


if __name__ == "__main__":
    main()
