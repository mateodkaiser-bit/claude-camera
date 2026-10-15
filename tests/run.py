#!/usr/bin/env python3
"""Regression tests for the camera skill. No camera, no printer needed: recorded fixtures are replayed
through `--input`, synthetic videos are made with ffmpeg's lavfi, and printers are mocked over HTTP.

  python3 tests/run.py            all tests (~2 min)
  python3 tests/run.py -k api     a subset (unittest -k)
  CAM_DIR=~/.claude/skills/camera python3 tests/run.py   test a working copy instead of this package

Fixtures (tests/fixtures, 640x480 at 2 fps, recorded 15 Oct 2026 on an Ultimaker S5, iPhone 13 mini):
  locate.mp4     40 s of a print in progress: three first-layer plates + a ring, white head, rods
  unmoved.mp4    20 s later, same view                                -> no shift, no "moved"
  bumped.mp4     the phone nudged (+8,+6 px at 320x240)               -> the region follows the shift
  different.mp4  a view rotated 180 degrees (stands in for a re-aim)  -> CAMERA MOVED
  blurred.mp4    autofocus hunting, everything soft                   -> BLURRY (after a sharp shot)
"""
import hashlib, http.server, json, os, re, shutil, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures"
CAM_DIR = Path(os.environ.get("CAM_DIR") or HERE.parent / "skills" / "camera").expanduser()
CAM = CAM_DIR / "cam.py"
sys.path.insert(0, str(CAM_DIR))
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

# where the parts are in locate.mp4, in the 320x240 analysis frame (x0, y0, x1, y1)
PLATES = [(184, 194, 227, 222), (184, 205, 243, 239), (219, 218, 257, 239)]
RING = (284, 215, 309, 232)


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]) + 1)
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]) + 1)
    area = lambda r: (r[2] - r[0] + 1) * (r[3] - r[1] + 1)
    inter = ix * iy
    return inter / (area(a) + area(b) - inter)


class Cam:
    """Runs cam.py with its own state and output folders."""
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="camtest-"))
        self.env = {**os.environ, "CLAUDE_CAM_STATE": str(self.tmp / "state")}

    def run(self, *a, timeout=180, env=None):
        p = subprocess.run([sys.executable, "-u", str(CAM), *a, "--out", str(self.tmp / "out")],
                           capture_output=True, text=True, timeout=timeout, env={**self.env, **(env or {})})
        return p.returncode, p.stdout + p.stderr

    def state(self, name):
        return json.loads((self.tmp / "state" / "file" / name).read_text())

    def snapshot(self):
        shutil.copytree(self.tmp / "state", self.tmp / "state0", dirs_exist_ok=True)

    def restore(self):
        shutil.rmtree(self.tmp / "state", ignore_errors=True)
        shutil.copytree(self.tmp / "state0", self.tmp / "state")


def pick_ids(cands, targets, min_iou=0.3):
    ids = []
    for k, c in enumerate(cands, 1):
        if any(iou(c["box"], t) >= min_iou for t in targets):
            ids.append(k)
    return ids


# ---------------------------------------------------------------- recorded fixtures
class PrintFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cam = Cam()
        code, cls.locate_out = cls.cam.run("print", "locate", "--input", str(FIX / "locate.mp4"))
        assert code == 0, cls.locate_out
        cls.cands = cls.cam.state("candidates.json")
        ids = pick_ids(cls.cands, PLATES + [RING])
        assert ids, cls.locate_out
        code, out = cls.cam.run("print", "use", ",".join(map(str, ids)), "--input", "x")
        assert code == 0, out
        cls.cam.snapshot()

    def setUp(self):
        self.cam.restore()

    def shot(self, name):
        return self.cam.run("print", "shot", "--input", str(FIX / name), "--max-wait", "30")

    def test_locate_reads_all_frames(self):
        self.assertIn("of 80 frames", self.locate_out)

    def test_locate_finds_large_flat_plates(self):
        found = [p for p in PLATES if any(iou(c["box"], p) >= 0.3 for c in self.cands)]
        self.assertGreaterEqual(len(found), 3, f"plates boxed: {found}\n{self.locate_out}")
        self.assertTrue(any(iou(c["box"], RING) >= 0.5 for c in self.cands), self.locate_out)

    def test_light_bed_not_inverted(self):
        self.assertNotIn("DARK BED", self.locate_out)

    def test_unmoved_no_shift(self):
        code, out = self.shot("unmoved.mp4")
        self.assertEqual(code, 0, out)
        self.assertNotIn("shifted", out)
        self.assertNotIn("CAMERA MOVED", out)

    def test_bumped_shift_followed(self):
        code, out = self.shot("bumped.mp4")
        self.assertEqual(code, 0, out)
        m = re.search(r"view shifted ([+-]\d+),([+-]\d+) px .*region followed it", out)
        self.assertTrue(m, out)
        dx, dy = int(m.group(1)), int(m.group(2))
        self.assertTrue(4 <= dx <= 12 and 2 <= dy <= 10, out)   # nudged ~+8,+6
        self.assertNotIn("CAMERA MOVED", out)

    def test_different_scene_camera_moved(self):
        code, out = self.shot("different.mp4")
        self.assertIn("CAMERA MOVED", out)

    def test_blurred_after_sharp_is_blurry(self):
        code, out = self.shot("unmoved.mp4")          # sets the sharpness reference
        self.assertEqual(code, 0, out)
        code, out = self.shot("blurred.mp4")
        self.assertIn("BLURRY", out)


# ---------------------------------------------------------------- synthetic scenes
def lavfi_video(path, bed, parts, head, part_grey, seconds=30):
    """bed grey, static parts (x, y, w, h at 640x480), a head box (w, h) that dwells over the first part
    60% of the time and roams otherwise."""
    px, py, pw, ph = parts[0]
    hx = f"if(lt(mod(t\\,10)\\,6)\\,{px - 10}\\,{px - 10}+200*sin(t))"
    hy = f"if(lt(mod(t\\,10)\\,6)\\,{py - 20}\\,60+40*cos(t))"
    boxes = ",".join(f"drawbox=x={x}:y={y}:w={w}:h={h}:color=0x{part_grey:02x}{part_grey:02x}{part_grey:02x}:t=fill"
                     for x, y, w, h in parts)
    vf = (f"{boxes},drawbox=x='{hx}':y='{hy}':w={head[0]}:h={head[1]}:color=white:t=fill,"
          f"noise=alls=6:allf=t,format=yuv420p")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c=0x{bed:02x}{bed:02x}{bed:02x}:s=640x480:r=2:d={seconds}",
                    "-vf", vf, "-c:v", "libx264", "-crf", "20", str(path)], check=True)


class Synthetic(unittest.TestCase):
    def test_light_print_on_dark_bed(self):
        cam = Cam()
        vid = cam.tmp / "dark.mp4"
        parts = [(260, 300, 120, 60), (420, 330, 70, 70)]
        lavfi_video(vid, bed=35, parts=parts, head=(140, 110), part_grey=205)
        code, out = cam.run("print", "locate", "--input", str(vid))
        self.assertEqual(code, 0, out)
        self.assertIn("DARK BED", out)
        cands = cam.state("candidates.json")
        targets = [(x // 2, y // 2, (x + w) // 2 - 1, (y + h) // 2 - 1) for x, y, w, h in parts]
        for t in targets:
            self.assertTrue(any(iou(c["box"], t) >= 0.5 for c in cands), f"{t} not found:\n{out}")
        ids = pick_ids(cands, targets, 0.5)
        code, out = cam.run("print", "use", ",".join(map(str, ids)), "--input", "x")
        self.assertEqual(code, 0, out)
        code, out = cam.run("print", "shot", "--input", str(vid), "--max-wait", "20")
        self.assertEqual(code, 0, out)
        vis = int(re.search(r"(\d+)% of the print's pixels visible", out).group(1))
        self.assertGreaterEqual(vis, 85, out)
        # forcing the wrong polarity finds nothing like the parts
        code, out = cam.run("print", "locate", "--input", str(vid), "--invert", "off")
        self.assertNotIn("DARK BED", out)

    def test_plate_with_rod_smear_is_split(self):
        import cam_print as P
        SW, SH = P.SW, P.SH
        img = [180] * (SW * SH)
        for y in range(150, 180):            # a compact plate, 50 x 30
            for x in range(100, 150):
                img[y * SW + x] = 40
        for t in range(0, 140):              # a 5 px rod smear running into it
            x, y = 10 + t, 60 + int(t * 0.7)
            for w in range(5):
                if y + w < SH:
                    img[(y + w) * SW + x] = 60
        cands = P.find_candidates(img)
        plate = (100, 150, 149, 179)
        self.assertTrue(any(iou(c["box"], plate) >= 0.7 for c in cands),
                        [c["box"] for c in cands])
        for c in cands:                      # no box spans the smear's far end
            self.assertFalse(c["box"][0] < 40 and c["box"][2] > 100, c["box"])


# ---------------------------------------------------------------- keep-alive stream
class KeepAlive(unittest.TestCase):
    def args(self, inp):
        import argparse
        return argparse.Namespace(input=str(inp), cam="phone", size=None)

    def test_reconnects_when_stream_ends(self):
        import cam as C
        os.environ["CLAUDE_CAM_RECONNECT_FILES"] = "1"
        os.environ["CLAUDE_CAM_BACKOFF"] = "0.01"
        logs = []
        try:
            live = C.LiveStream(self.args(FIX / "blurred.mp4"), None,
                                [("scale=64:48,format=gray", "s%06d.pgm"), ("null", "f%06d.jpg")],
                                fps=2, tag="camtest-live-", log=logs.append)
            n = sum(1 for _ in live.frames(time.time() + 12))
            live.close()
        finally:
            del os.environ["CLAUDE_CAM_RECONNECT_FILES"], os.environ["CLAUDE_CAM_BACKOFF"]
        self.assertGreater(n, 50, logs)              # one pass is 40 frames: it kept going
        self.assertGreaterEqual(live.reconnects, 1)
        self.assertTrue(any("reconnecting" in m for m in logs), logs)
        self.assertFalse(live.root.exists())

    def test_detects_stall(self):
        """A stream that opens but never delivers (the 15 Oct "no frames" stall) is restarted."""
        import cam as C
        fifo = Path(tempfile.mkdtemp()) / "never"
        os.mkfifo(fifo)
        os.environ["CLAUDE_CAM_RECONNECT_FILES"] = "1"
        os.environ["CLAUDE_CAM_BACKOFF"] = "0.01"
        logs = []
        try:
            live = C.LiveStream(self.args(fifo), None, [("format=gray", "s%06d.pgm")], fps=2,
                                tag="camtest-stall-", stall=1, log=logs.append)
            n = sum(1 for _ in live.frames(time.time() + 10))
            live.close()
        finally:
            del os.environ["CLAUDE_CAM_RECONNECT_FILES"], os.environ["CLAUDE_CAM_BACKOFF"]
        self.assertEqual(n, 0)
        self.assertTrue(any("stalled" in m for m in logs), logs)

    def test_drain_keeps_disk_flat(self):
        import cam as C
        live = C.LiveStream(self.args(FIX / "unmoved.mp4"), None,
                            [("scale=64:48,format=gray", "s%06d.pgm"), ("null", "f%06d.jpg")],
                            fps=2, tag="camtest-drain-", log=lambda m: None)
        time.sleep(3)
        newest = live.drain()
        left = list(live.dir.iterdir())
        live.close()
        self.assertIsNotNone(newest)
        self.assertLessEqual(len(left), 4, left)   # newest frame (+ maybe one in progress), both outputs

    def test_timelapse_keepalive(self):
        cam = Cam()
        code, out = cam.run("timelapse", "--input", str(FIX / "unmoved.mp4"), "--every", "2s", "--for", "7s",
                            "--width", "160", env={"CLAUDE_CAM_RECONNECT_FILES": "1", "CLAUDE_CAM_BACKOFF": "0.05"})
        self.assertEqual(code, 0, out)
        self.assertIn("kept connected", out)
        self.assertGreaterEqual(len(list((cam.tmp / "out").glob("timelapse-*.jpg"))), 2, out)


# ---------------------------------------------------------------- printer APIs (mocked)
class MockPrinter(http.server.BaseHTTPRequestHandler):
    kind, t0, finish_after, key, digest = "ultimaker", 0.0, 1e9, None, None

    def log_message(self, *a):
        pass

    def send(self, code, obj=None, headers=()):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        for h in headers:
            self.send_header(*h)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorised(self):
        if self.key and self.headers.get("X-Api-Key") != self.key:
            self.send(401, {"error": "key"})
            return False
        if self.digest:
            user, pw = self.digest
            a = self.headers.get("Authorization", "")
            if not a.startswith("Digest "):
                self.send(401, None, [("WWW-Authenticate", 'Digest realm="Printer API", nonce="n0nce", qop="auth"')])
                return False
            f = dict(re.findall(r'(\w+)="?([^",]+)"?', a[7:]))
            ha1 = hashlib.md5(f"{user}:Printer API:{pw}".encode()).hexdigest()
            ha2 = hashlib.md5(f"GET:{f.get('uri')}".encode()).hexdigest()
            want = hashlib.md5(f"{ha1}:n0nce:{f.get('nc')}:{f.get('cnonce')}:auth:{ha2}".encode()).hexdigest()
            if f.get("username") != user or f.get("response") != want:
                self.send(401, None, [("WWW-Authenticate", 'Digest realm="Printer API", nonce="n0nce", qop="auth"')])
                return False
        return True

    def do_GET(self):
        if not self.authorised():
            return
        t = time.time() - self.t0
        done = t > self.finish_after
        prog = min(1.0, t / max(1, self.finish_after))
        x, y, z = 100 + 80 * ((int(t) % 4) // 2), 100, 0.2 * (1 + int(t / 3))
        if self.kind == "ultimaker":
            if self.path == "/api/v1/print_job":
                return self.send(200, {"state": "post_print" if done else "printing", "progress": prog,
                                       "time_elapsed": int(t), "time_total": 600, "result": ""})
            if self.path == "/api/v1/printer/heads/0/position":
                return self.send(200, {"x": x, "y": y, "z": z})
        if self.kind == "prusalink" and self.path == "/api/v1/status":
            return self.send(200, {"printer": {"state": "FINISHED" if done else "PRINTING", "axis_z": z,
                                               "axis_x": x, "axis_y": y},
                                   "job": {"id": 1, "progress": prog * 100, "time_remaining": 300}})
        if self.kind == "octoprint" and self.path == "/api/job":
            return self.send(200, {"state": "Operational" if done else "Printing",
                                   "progress": {"completion": prog * 100, "printTimeLeft": 300}})
        self.send(404, {"error": "no such endpoint"})


def serve(kind, **kw):
    handler = type("H", (MockPrinter,), dict(kind=kind, t0=time.time(), **kw))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"127.0.0.1:{srv.server_address[1]}"


class PrinterAPI(unittest.TestCase):
    def setUp(self):
        self.cam = Cam()
        os.environ["CLAUDE_CAM_STATE"] = self.cam.env["CLAUDE_CAM_STATE"]
        import cam_printer
        cam_printer.STATE = Path(self.cam.env["CLAUDE_CAM_STATE"])
        cam_printer.CONFIG = cam_printer.STATE / "printers.json"
        self.cp = cam_printer

    def write_creds(self, host, **creds):
        self.cp.CONFIG.parent.mkdir(parents=True, exist_ok=True)
        self.cp.CONFIG.write_text(json.dumps({host: creds}))

    def test_api_ultimaker_parse(self):
        srv, host = serve("ultimaker")
        r = self.cp.Printer("ultimaker", host).poll()
        srv.shutdown()
        self.assertEqual(r["state"], "printing")
        self.assertEqual((r["x"], r["y"]), (100, 100))
        self.assertIsNotNone(r["z"])

    def test_api_prusalink_digest(self):
        srv, host = serve("prusalink", digest=("maker", "s3cret"))
        self.write_creds(host, user="maker", password="s3cret", api_key="")
        r = self.cp.Printer("prusalink", host).poll()
        self.write_creds(host, user="maker", password="wrong", api_key="")
        with self.assertRaises(PermissionError):
            self.cp.Printer("prusalink", host).poll()
        srv.shutdown()
        self.assertEqual(r["state"], "printing")
        self.assertAlmostEqual(r["progress"], 0.0, delta=0.05)

    def test_api_octoprint_key(self):
        srv, host = serve("octoprint", key="k3y", finish_after=0)
        self.write_creds(host, api_key="k3y")
        r = self.cp.Printer("octoprint", host).poll()
        srv.shutdown()
        self.assertEqual(r["state"], "idle")
        self.assertEqual(r["raw"], "Operational")

    def test_api_unreachable_is_not_fatal(self):
        r = self.cp.Printer("ultimaker", "127.0.0.1:9", timeout=1).poll()
        self.assertEqual(r["state"], "unreachable")

    def test_api_head_context(self):
        p = self.cp.Printer("ultimaker", "x")
        now = time.time()
        p.samples = [(now - 10 + i, 100, 100, 1.0) for i in range(8)]
        p.last = dict(t=now, x=180, y=100, z=1.2, state="printing")
        p.samples.append((now, 180, 100, 1.2))
        away, layer = p.head_context()
        self.assertEqual(away, 80)
        self.assertTrue(layer)

    def test_printer_setup_template(self):
        code, out = self.cam.run("print", "printer-setup", "--printer", "prusalink", "--host", "10.0.0.9")
        self.assertEqual(code, 0, out)
        cfg = Path(self.cam.env["CLAUDE_CAM_STATE"]) / "printers.json"
        self.assertEqual(oct(cfg.stat().st_mode & 0o777), "0o600")
        self.assertEqual(json.loads(cfg.read_text())["10.0.0.9"]["password"], "")
        self.assertIn("YOU:", out)

    def test_monitor_with_printer_finishes(self):
        """Monitor + mocked Ultimaker: progress/Z in the log, and a final shot + stop when the job ends."""
        cam = self.cam
        code, out = cam.run("print", "locate", "--input", str(FIX / "locate.mp4"))
        ids = pick_ids(cam.state("candidates.json"), PLATES + [RING])
        cam.run("print", "use", ",".join(map(str, ids)), "--input", "x")
        srv, host = serve("ultimaker", finish_after=14)
        code, out = cam.run("print", "monitor", "--input", str(FIX / "unmoved.mp4"), "--every", "10s",
                            "--for", "2m", "--printer", "ultimaker", "--host", host, "--layer-height", "0.2",
                            env={"CLAUDE_CAM_RECONNECT_FILES": "1", "CLAUDE_CAM_BACKOFF": "0.05"}, timeout=240)
        srv.shutdown()
        self.assertEqual(code, 0, out)
        self.assertIn("kept connected", out)
        self.assertRegex(out, r"printer printing, \d+%, Z [\d.]+ mm \(~layer \d+\)")
        self.assertIn("JOB FINISHED", out)
        self.assertIn("full view:", out)
        self.assertIn("monitor stopped: the print finished", out)
        self.assertTrue(list((cam.tmp / "out").glob("monitor-final-print-*-full.jpg")), out)


if __name__ == "__main__":
    if not CAM.exists():
        sys.exit(f"cam.py not found at {CAM}")
    print(f"testing {CAM_DIR}", file=sys.stderr)
    import warnings
    warnings.simplefilter("ignore", ResourceWarning)   # mock-server sockets; not the code under test
    unittest.main(verbosity=2)
