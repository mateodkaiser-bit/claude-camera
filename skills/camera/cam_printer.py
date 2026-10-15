"""Optional printer APIs for `print monitor`: job state, progress and head position over the LAN.

  cam.py print printer-setup --printer prusalink --host 192.168.1.40   writes a config template to fill in
  cam.py print printer-test  --printer prusalink --host 192.168.1.40   shows what the printer answers

Endpoints, from the vendors' own documentation (checked 15 Oct 2026):
  ultimaker  GET http://<ip>/api/v1/print_job               state, progress (0-1 fraction), time_elapsed, time_total
             GET http://<ip>/api/v1/printer/heads/0/position {x, y, z} in mm
             No key is needed to READ on Ultimaker printers (only writes need digest auth).
  prusalink  GET http://<ip>/api/v1/status                  printer.state (IDLE/BUSY/PRINTING/PAUSED/FINISHED/
             STOPPED/ERROR/ATTENTION/READY), printer.axis_x/y (only while not moving), axis_z,
             job.progress (percent), job.time_remaining (s). HTTP digest auth: user + password from the
             printer's settings screen (older firmware: X-Api-Key, also supported).
  octoprint  GET http://<ip>/api/job                        state ("Printing", "Operational", ...),
             progress.completion (percent), progress.printTimeLeft (s). X-Api-Key header.
             OctoPrint exposes no head position or layer without plugins, so it gives state + progress only.

Credentials live in ~/.claude/camera-state/printers.json (mode 600), keyed by host, filled in by the user
in a text editor. They are never passed on the command line or pasted into the chat.
Stdlib only (urllib).
"""
import json, os, statistics, sys, threading, time, urllib.error, urllib.request
from pathlib import Path

STATE = Path(os.environ.get("CLAUDE_CAM_STATE") or Path.home() / ".claude" / "camera-state")
CONFIG = STATE / "printers.json"
KINDS = ("ultimaker", "prusalink", "octoprint")
TEMPLATE = {
    "ultimaker": {"note": "Ultimaker: reading needs no key. Nothing to fill in."},
    "prusalink": {"user": "maker", "password": "", "api_key": "",
                  "note": "Printer screen > Settings > Network > PrusaLink: user + password (digest). "
                          "Older firmware shows an API key instead: fill api_key and leave password empty."},
    "octoprint": {"api_key": "",
                  "note": "OctoPrint > Settings > Application Keys (or API): create a key, paste it here."},
}


def load_creds(host):
    try:
        return json.loads(CONFIG.read_text()).get(host, {})
    except (OSError, ValueError):
        return {}


def cmd_printer_setup(args):
    if args.printer not in KINDS or not args.host:
        sys.exit(f"usage: print printer-setup --printer {'|'.join(KINDS)} --host <ip>")
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    try:
        cfg = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        cfg = {}
    entry = cfg.get(args.host) or {"printer": args.printer, **TEMPLATE[args.printer]}
    cfg[args.host] = entry
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")
    os.chmod(CONFIG, 0o600)
    empty = [k for k, v in entry.items() if k != "note" and k != "printer" and v == ""]
    print(f"printer config: {CONFIG} (only you can read it)")
    if args.printer == "ultimaker":
        print("Ultimaker needs no credentials to read status. Next: cam.py print printer-test "
              f"--printer ultimaker --host {args.host}")
    else:
        print(f"YOU: open that file in a text editor and fill in {', '.join(empty) or 'the fields'} for "
              f"{args.host}. {entry['note']}\n  Don't paste the key into the chat. Then: cam.py print "
              f"printer-test --printer {args.printer} --host {args.host}")


class Printer:
    """Polls one printer. `poll()` -> dict(state, progress 0-1 or None, x, y, z, raw)."""
    def __init__(self, kind, host, timeout=4):
        if kind not in KINDS:
            sys.exit(f"--printer must be one of {', '.join(KINDS)}")
        self.kind, self.host, self.timeout = kind, host, timeout
        self.base = host if host.startswith("http") else f"http://{host}"
        creds = load_creds(host)
        self.headers = {"Accept": "application/json"}
        handlers = []
        if creds.get("api_key"):
            self.headers["X-Api-Key"] = creds["api_key"]
        elif kind == "prusalink" and creds.get("password"):
            pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            pm.add_password(None, self.base, creds.get("user", "maker"), creds["password"])
            handlers.append(urllib.request.HTTPDigestAuthHandler(pm))
        elif kind != "ultimaker":
            print(f"NOTE: no credentials for {host} in {CONFIG}; run `print printer-setup` if the printer "
                  "answers 401.", file=sys.stderr)
        self.opener = urllib.request.build_opener(*handlers)
        self.samples = []            # (t, x, y, z): head track, for "is the head away from the print"
        self.last = None
        self._lock = threading.Lock()
        self._thread = None

    def _get(self, path):
        req = urllib.request.Request(self.base + path, headers=self.headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                body = r.read()
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as e:
            if e.code in (204, 404, 409):        # no job (Ultimaker 404, OctoPrint 409 when offline)
                return None
            if e.code in (401, 403):
                raise PermissionError(f"{self.kind} at {self.host} refused the credentials (HTTP {e.code}); "
                                      f"check {CONFIG}") from None
            raise

    def poll(self):
        """One reading. Never raises for network trouble: returns state 'unreachable' instead."""
        try:
            r = getattr(self, "_poll_" + self.kind)()
        except PermissionError:
            raise
        except (OSError, ValueError, urllib.error.URLError) as e:
            r = dict(state="unreachable", progress=None, x=None, y=None, z=None, raw=str(e)[:120])
        r["t"] = time.time()
        with self._lock:
            self.last = r
            if r.get("x") is not None and r.get("y") is not None:
                self.samples = (self.samples + [(r["t"], r["x"], r["y"], r.get("z"))])[-900:]
        return r

    # --- one parser per vendor; all map onto: printing | paused | finished | idle | error | unknown
    def _poll_ultimaker(self):
        job = self._get("/api/v1/print_job")
        pos = self._get("/api/v1/printer/heads/0/position") or {}
        raw_state = (job or {}).get("state", "none")
        state = {"printing": "printing", "pre_print": "printing", "resuming": "printing",
                 "pausing": "paused", "paused": "paused", "post_print": "finished",
                 "wait_cleanup": "finished", "wait_user_action": "paused", "none": "idle"}.get(raw_state, "unknown")
        if job and str(job.get("result", "")).lower() in ("finished", "aborted", "failed") and state != "printing":
            state = "finished"
        return dict(state=state, raw=raw_state, progress=(job or {}).get("progress"),
                    left=(job["time_total"] - job["time_elapsed"]) if job and job.get("time_total") else None,
                    x=pos.get("x"), y=pos.get("y"), z=pos.get("z"))

    def _poll_prusalink(self):
        st = self._get("/api/v1/status") or {}
        p, job = st.get("printer", {}), st.get("job") or {}
        raw_state = p.get("state", "")
        state = {"PRINTING": "printing", "PAUSED": "paused", "FINISHED": "finished", "STOPPED": "finished",
                 "IDLE": "idle", "READY": "idle", "BUSY": "printing", "ERROR": "error",
                 "ATTENTION": "paused"}.get(raw_state, "unknown")
        prog = job.get("progress")
        return dict(state=state, raw=raw_state, progress=prog / 100 if prog is not None else None,
                    left=job.get("time_remaining"), x=p.get("axis_x"), y=p.get("axis_y"), z=p.get("axis_z"))

    def _poll_octoprint(self):
        j = self._get("/api/job") or {}
        raw_state = j.get("state", "")
        s = raw_state.lower()
        state = ("printing" if s.startswith("printing") or s in ("cancelling", "finishing", "starting")
                 else "paused" if s.startswith("paus") else "error" if "error" in s
                 else "idle" if s in ("operational", "ready") else "unknown")
        prog = (j.get("progress") or {})
        comp = prog.get("completion")
        return dict(state=state, raw=raw_state, progress=comp / 100 if comp is not None else None,
                    left=prog.get("printTimeLeft"), x=None, y=None, z=None)

    # --- background sampling while a shot window runs
    def start_sampling(self, every=1.0):
        self._stop = threading.Event()
        def loop():
            while not self._stop.is_set():
                try:
                    self.poll()
                except PermissionError:
                    return
                self._stop.wait(every)
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop_sampling(self):
        if self._thread:
            self._stop.set()
            self._thread.join(self.timeout + 1)
            self._thread = None

    def head_context(self):
        """(away_mm, layer_changed) for the latest sample. away = distance from where the head has spent
        most of this job (the median of its track). The head dwells over the part it is printing (the
        same observation that makes `locate` use the 25th percentile), so far from that point = off the print."""
        with self._lock:
            s, last = list(self.samples), self.last
        if not last or last.get("x") is None or len(s) < 5:
            return None, False
        cx, cy = statistics.median(p[1] for p in s), statistics.median(p[2] for p in s)
        away = ((last["x"] - cx) ** 2 + (last["y"] - cy) ** 2) ** 0.5
        recent = [p[3] for p in s if p[3] is not None and last["t"] - p[0] <= 3]
        layer = bool(recent) and last.get("z") is not None and last["z"] - min(recent) > 0.02
        return round(away), layer

    def describe(self, r=None, layer_height=None):
        r = r or self.last
        if not r:
            return ""
        bits = [f"printer {r['state']}" + (f" ({r['raw']})" if r.get("raw") and r['raw'].lower() != r['state'] else "")]
        if r.get("progress") is not None:
            bits.append(f"{r['progress']:.0%}")
        if r.get("z") is not None:
            bits.append(f"Z {r['z']:.2f} mm" + (f" (~layer {round(r['z'] / layer_height)})" if layer_height else ""))
        if r.get("left"):
            bits.append(f"{r['left'] / 60:.0f} min left")
        return ", ".join(bits)


def cmd_printer_test(args):
    p = Printer(args.printer, args.host)
    try:
        r = p.poll()
    except PermissionError as e:
        sys.exit(f"PRINTER REFUSED: {e}")
    print(json.dumps({k: v for k, v in r.items() if k != "t"}, indent=1))
    print(p.describe(r, getattr(args, "layer_height", None)))
    if r["state"] == "unreachable":
        sys.exit("NOT REACHABLE: is the printer on the same network, and is the IP right? "
                 "(Ultimaker: Preferences > Network on the touchscreen; Prusa: Settings > Network)")
