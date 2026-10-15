"""Find a dark print on a light bed and photograph it when the print head is out of the way.

  cam.py print locate [--seconds 40]   record, build the head-free median, box dark-on-bed candidates
  cam.py print use 1,3                 save the union of those candidates as the print region (ROI)
  cam.py print shot [--max-wait 120]   stream until the ROI is clear of the head, save a full-res crop

Why it works: the head moves and the print doesn't, so the per-pixel MEDIAN over ~40 s is the
bed without the head ("clean plate"). Dark blobs that contrast with their local surroundings in
that median are print candidates. Which candidates are print is a semantic call (belts, clips and
logos are dark too), so a human or Claude confirms once from the annotated image; after that,
`shot` is pure pixel arithmetic: a frame is "clear" when the ROI matches the clean plate.
Stdlib only; analysis runs at 320x240 grey, the saved crop comes from the 1920x1440 frame.
"""
import json, shutil, subprocess, sys, tempfile, time
from pathlib import Path

import cam as C

SW, SH = 320, 240                       # analysis resolution
STATE = Path.home() / ".claude" / "camera-state"
COLORS = ["red", "cyan", "magenta", "lime", "orange", "blue", "yellow", "white"]


# ---------------------------------------------------------------- streaming
def stream(args, device, work, fps=2):
    """ffmpeg -> work/sNNNNNN.pgm (320x240 grey) + work/fNNNNNN.jpg (full res), `fps` per second."""
    warm_s = 0 if args.input else (C.WARMUP_PHONE if "iphone" in device.lower() else C.WARMUP_FRAMES) / 30
    fc = (f"[0:v]trim=start={warm_s},setpts=PTS-STARTPTS,fps={fps},split[a][b];"
          f"[a]scale={SW}:{SH},format=gray[s];[b]null[f]")
    return C.ff_start(["-hide_banner", "-loglevel", "error", "-y", *C.input_args(args, device),
                       "-filter_complex", fc, "-map", "[s]", str(work / "s%06d.pgm"),
                       "-map", "[f]", "-q:v", "2", str(work / "f%06d.jpg")], camera=not args.input)


def frames(proc, work, deadline, stall=25):
    """Yield (grey bytes, jpg path) for each completed frame until deadline or the stream ends.
    Gives up after `stall` s with no frame at all: a stalled Continuity stream otherwise eats the window."""
    seen, t0 = 0, time.time()
    while True:
        if seen == 0 and time.time() - t0 > stall + 6:   # +6: the phone's warm-up is trimmed off
            return
        ended = proc.poll() is not None
        fs = sorted(work.glob("s*.pgm"))
        for f in fs[seen:] if ended else fs[seen:-1]:
            seen += 1
            jpg = work / f.name.replace("s", "f", 1).replace(".pgm", ".jpg")
            for _ in range(20):                  # the jpg of the same frame may land a moment later
                if jpg.exists() and jpg.stat().st_size > 0:
                    break
                time.sleep(0.1)
            yield C.read_pgm(f)[2], jpg
        if ended or time.time() > deadline:
            return
        time.sleep(0.25)


def state_dir(device):
    d = STATE / "".join(ch if ch.isalnum() else "_" for ch in (device or "file"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_pgm(path, px):
    Path(path).write_bytes(b"P5\n%d %d\n255\n" % (SW, SH) + bytes(px))


# ---------------------------------------------------------------- image analysis (pure Python)
def local_mean(img, r):
    """Box mean with radius r via an integral image."""
    ii = [0] * ((SW + 1) * (SH + 1))
    for y in range(SH):
        row = 0
        for x in range(SW):
            row += img[y * SW + x]
            ii[(y + 1) * (SW + 1) + x + 1] = ii[y * (SW + 1) + x + 1] + row
    out = [0] * (SW * SH)
    for y in range(SH):
        y0, y1 = max(0, y - r), min(SH, y + r + 1)
        for x in range(SW):
            x0, x1 = max(0, x - r), min(SW, x + r + 1)
            s = ii[y1 * (SW + 1) + x1] - ii[y0 * (SW + 1) + x1] - ii[y1 * (SW + 1) + x0] + ii[y0 * (SW + 1) + x0]
            out[y * SW + x] = s / ((y1 - y0) * (x1 - x0))
    return out


def morph(mask, op):
    f = all if op == "erode" else any
    return [f(mask[(y + dy) * SW + x + dx] for dy in (-1, 0, 1) for dx in (-1, 0, 1)
              if 0 <= x + dx < SW and 0 <= y + dy < SH)
            for y in range(SH) for x in range(SW)]


def components(mask):
    seen, out = [False] * len(mask), []
    for s, m in enumerate(mask):
        if not m or seen[s]:
            continue
        stack, px = [s], []
        seen[s] = True
        while stack:
            i = stack.pop()
            px.append(i)
            x, y = i % SW, i // SW
            for j, ok in ((i - 1, x > 0), (i + 1, x < SW - 1), (i - SW, y > 0), (i + SW, y < SH - 1)):
                if ok and mask[j] and not seen[j]:
                    seen[j] = True
                    stack.append(j)
        out.append(px)
    return out


def find_candidates(med):
    lm = local_mean(med, 12)
    # local contrast finds small parts; the absolute test finds LARGE solid parts (first-layer plates),
    # which fill the 25 px window and so never look darker than their own neighbourhood (15 Oct)
    dark = [(m < 140 and m < l - 30) or m < 70 for m, l in zip(med, lm)]
    # opening (erode once, dilate once): removes 1-2 px specks/edges; belts are handled by the
    # thickness test below, which works at any angle (a bbox-aspect test misses diagonal belts)
    core = morph(morph(dark, "erode"), "dilate")
    cands = []
    for px in components(core):
        if len(px) < 30:            # specks and logo letters (12-24 px here); real parts were 110+ px
            continue
        xs, ys = [i % SW for i in px], [i // SW for i in px]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        bw, bh = x1 - x0 + 1, y1 - y0 + 1
        thickness = len(px) / max(bw, bh)                  # area / length: ~line width for strokes
        if thickness < 3.5 and max(bw, bh) > 25:           # long and thin: belt, rail edge, rod shadow
            continue
        contrast = sum(lm[i] - med[i] for i in px) / len(px)
        # "on the bed": mean brightness of a 4 px ring around the box in the head-free median
        ring = [med[y * SW + x] for y in range(max(0, y0 - 4), min(SH, y1 + 5))
                for x in range(max(0, x0 - 4), min(SW, x1 + 5)) if not (x0 <= x <= x1 and y0 <= y <= y1)]
        bed = sum(ring) / len(ring) / 255 if ring else 0
        cands.append(dict(box=[x0, y0, x1, y1], area=len(px), contrast=round(contrast), bed=round(bed, 2),
                          score=round(len(px) * contrast * bed ** 2), px=px))
    # being ON the bright bed is what separates print from hardware (belts, clips, rails are dark too),
    # so it is the primary key (0.1 buckets); size x contrast breaks ties
    cands.sort(key=lambda c: (-round(c["bed"], 1), -c["score"]))
    return cands[:len(COLORS)]


def down(img, f):
    """Average f x f blocks of a SW x SH grey image -> (w, h, pixels)."""
    w, h = SW // f, SH // f
    return w, h, [sum(img[(y * f + j) * SW + x * f + i] for j in range(f) for i in range(f)) / (f * f)
                  for y in range(h) for x in range(w)]


def _err(a, b, w, h, dx, dy):
    """Mean abs difference (each image mean-removed, so exposure drift doesn't count) over the overlap
    when b is a shifted by (dx, dy). None if the overlap is under half the frame."""
    xs, ys = range(max(0, -dx), min(w, w - dx)), range(max(0, -dy), min(h, h - dy))
    if len(xs) * len(ys) < w * h / 2:
        return None
    pa = [a[y * w + x] for y in ys for x in xs]
    pb = [b[(y + dy) * w + x + dx] for y in ys for x in xs]
    ma, mb = sum(pa) / len(pa), sum(pb) / len(pb)
    # Trimmed: drop the 40% of pixels that differ most (wherever the head/rods happen to be). A close
    # phone sees the head over half the frame, and the plain mean called that a "move" (15 Oct, 25.9).
    # A real re-aim changes nearly every pixel, so it still scores high.
    d = sorted(abs((u - ma) - (v - mb)) for u, v in zip(pa, pb))
    keep = d[:max(1, int(len(d) * 0.6))]
    return sum(keep) / len(keep)


def estimate_shift(ref, cur):
    """How far the view moved: content at ref(x, y) is now at cur(x + dx, y + dy), in 320x240 px.
    Coarse search at 40x30 (up to +-80 / +-56 px), refined at 160x120. Returns (dx, dy, err0, err)."""
    w, h, a = down(ref, 8); _, _, b = down(cur, 8)
    best = min(((e, dx, dy) for dx in range(-10, 11) for dy in range(-7, 8)
                if (e := _err(a, b, w, h, dx, dy)) is not None))
    w2, h2, a2 = down(ref, 2); _, _, b2 = down(cur, 2)
    cx, cy = best[1] * 4, best[2] * 4
    fine = min(((e, dx, dy) for dx in range(cx - 4, cx + 5) for dy in range(cy - 4, cy + 5)
                if (e := _err(a2, b2, w2, h2, dx, dy)) is not None))
    return fine[1] * 2, fine[2] * 2, round(_err(a2, b2, w2, h2, 0, 0) or 99, 1), round(fine[0], 1)


def shift_img(img, dx, dy):
    """Move image content by (dx, dy); exposed edges copy the nearest pixel."""
    return [img[min(SH - 1, max(0, y - dy)) * SW + min(SW - 1, max(0, x - dx))] for y in range(SH) for x in range(SW)]


# Trimmed grey error (best 60% of pixels), calibrated 15 Oct. Recordings: unmoved 3.2, bumped and aligned
# 4.6 (improvement 4.3), different scene 12.0. Live close-up view, head-heavy, unmoved: 12.5, and the search
# proposed a BOGUS shift (+88,-64) improving only 3.7. Hence two guards:
SHIFT_SUSPECT = 9   # above = maybe re-aimed; only "moved" if the print is ALSO not visible this round
SHIFT_GAIN = 4      # follow a shift only if it improves the match this much...
SHIFT_MAX = 60      # ...and is at most this many px (320x240 scale): a wrong shift is worse than none


def cell(x, y):
    """Grid cell on the 8x6 locate grid: columns A-H left->right, rows 1-6 top->bottom."""
    return f"{chr(65 + min(7, x * 8 // SW))}{1 + min(5, y * 6 // SH)}"


# ---------------------------------------------------------------- commands
def cmd_locate(args):
    device = None if args.input else C.resolve(args.cam)
    work = Path(tempfile.mkdtemp(prefix="cam-locate-"))
    proc, err, pidf = stream(args, device, work)
    print(f"recording {args.seconds:.0f} s of '{device or args.input}' to build the head-free view...",
          file=sys.stderr, flush=True)
    try:
        grey = [g for g, _ in frames(proc, work, time.time() + args.seconds + 8)]
    finally:
        C.ff_stop(proc, pidf)
    if len(grey) < 10:
        shutil.rmtree(work, ignore_errors=True)
        sys.exit(f"only {len(grey)} frames arrived. {C.ff_err(err)[-300:]}")
    n = len(grey)
    med, low = [0] * (SW * SH), [0] * (SW * SH)
    for i in range(SW * SH):
        col = sorted(g[i] for g in grey)
        med[i], low[i] = col[n // 2], col[n // 4]
    sd = state_dir(device)
    write_pgm(sd / "median.pgm", med)   # camera-moved check
    # Detect on the 25th percentile, not the median: the head dwells over the part it is printing,
    # often >50% of the time (seen 15 Oct: the median there was mostly head). The print only has to
    # be uncovered a quarter of the time to show up dark at p25; the white head never makes a pixel darker.
    write_pgm(sd / "low.pgm", low)
    cands = find_candidates(low)
    (sd / "candidates.json").write_text(json.dumps(cands))
    out = C.outdir(args) / f"{args.label + '-' if args.label else ''}print-locate-{C.ts()}.jpg"
    boxes = ",".join(f"drawbox=x={c['box'][0] * 2}:y={c['box'][1] * 2}:w={(c['box'][2] - c['box'][0] + 1) * 2}"
                     f":h={(c['box'][3] - c['box'][1] + 1) * 2}:color={COLORS[k]}:t=2"
                     for k, c in enumerate(cands))
    vf = f"scale={SW * 2}:{SH * 2},format=rgb24,drawgrid=w=iw/8:h=ih/6:t=1:c=yellow@0.35" + ("," + boxes if boxes else "")
    C.run_ffmpeg(["-hide_banner", "-loglevel", "error", "-y", "-i", str(sd / "low.pgm"), "-vf", vf,
                  "-q:v", "3", str(out)], camera=False)
    shutil.rmtree(work, ignore_errors=True)
    C.report(out, device, f"\n  print-visible composite (25th percentile) of {n} frames, grid 8x6 (A1 top-left, H6 bottom-right)")
    if not cands:
        print("  no dark-on-bed candidates found. Is the print in view and darker than the bed?")
    for k, c in enumerate(cands, 1):
        x0, y0, x1, y1 = c["box"]
        print(f"  {k} {COLORS[k - 1]:8s} cells {cell(x0, y0)}-{cell(x1, y1)}  area {c['area']} px  "
              f"contrast {c['contrast']}  on-bed {c['bed']}")
    print("  next: look at the image, then `cam.py print use <numbers>` for the boxes that are print")


def cmd_use(args):
    device = None if args.input else C.resolve(args.cam)
    sd = state_dir(device)
    cands = json.loads((sd / "candidates.json").read_text())
    pick = [cands[int(k) - 1] for k in args.ids.split(",")]
    x0 = min(c["box"][0] for c in pick); y0 = min(c["box"][1] for c in pick)
    x1 = max(c["box"][2] for c in pick); y1 = max(c["box"][3] for c in pick)
    roi = [x0, y0, x1, y1]
    low = C.read_pgm(sd / "low.pgm")[2]
    mask = sorted({i for c in pick for i in c["px"]})
    ring = [y * SW + x for y in range(max(0, y0 - 4), min(SH, y1 + 5)) for x in range(max(0, x0 - 4), min(SW, x1 + 5))
            if not (x0 <= x <= x1 and y0 <= y <= y1)]
    print_lvl = sum(low[i] for i in mask) / len(mask)
    bed_lvl = sum(low[i] for i in ring) / len(ring)
    thr = (print_lvl + bed_lvl) / 2       # a mask pixel darker than this = print visible there
    # Grow: the confirmed boxes are seeds. Neighbouring parts of the same plate are often too faint or
    # small to make the top-8 list, so add every dark-on-bed pixel near the seeds (within args.grow of
    # the frame) and drop specks. Region = bbox of the grown mask.
    g = int(args.grow * SW)
    lm = local_mean(low, 12)
    gx0, gy0, gx1, gy1 = max(0, x0 - g), max(0, y0 - g), min(SW - 1, x1 + g), min(SH - 1, y1 + g)
    near = [False] * (SW * SH)
    for y in range(gy0, gy1 + 1):
        for x in range(gx0, gx1 + 1):
            i = y * SW + x
            near[i] = low[i] < thr and low[i] < lm[i] - 25
    mask = sorted(set(mask) | {i for comp in components(near) if len(comp) >= 10 for i in comp})
    xs, ys = [i % SW for i in mask], [i // SW for i in mask]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    roi = [x0, y0, x1, y1]
    (sd / "sharp_ref.json").unlink(missing_ok=True)   # new region: sharpness reference starts over
    (sd / "roi.json").write_text(json.dumps({"roi": roi, "mask": mask, "thr": round(thr), "ids": args.ids,
                                             "levels": [round(print_lvl), round(bed_lvl)], "saved": C.ts()}))
    print(f"print region saved: cells {cell(x0, y0)}-{cell(x1, y1)} "
          f"({(x1 - x0 + 1) / SW:.0%} x {(y1 - y0 + 1) / SH:.0%} of the frame), {len(mask)} print px, print {print_lvl:.0f} vs bed {bed_lvl:.0f} grey. Now: cam.py print shot")


def cmd_shot(args):
    device = None if args.input else C.resolve(args.cam)
    sd = state_dir(device)
    if not (sd / "roi.json").exists():
        sys.exit("no print region saved for this camera: run `cam.py print locate`, then `print use`.")
    med = C.read_pgm(sd / "median.pgm")[2]
    st = json.loads((sd / "roi.json").read_text())
    x0, y0, x1, y1 = st["roi"]
    mask, thr = st["mask"], st["thr"]
    roi = [y * SW + x for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
    work = Path(tempfile.mkdtemp(prefix="cam-shot-"))
    proc, err, pidf = stream(args, device, work)
    print(f"waiting up to {args.max_wait:.0f} s for the head to clear the print...", file=sys.stderr, flush=True)
    best, prev, n, glob_match, max_sharp, frozen = None, None, 0, [], 0.0, 0
    warm, aligned, shift_note, moved_big, suspect = [], False, "", False, False
    # sharpness = mean neighbour-pixel difference in the print region. Autofocus hunting gave a
    # completely blurred "best" frame on 15 Oct, because visibility alone can't see blur.
    def sharpness(img):
        return sum(abs(img[i] - img[i + 1]) + abs(img[i] - img[i + SW]) for i in roi
                   if i % SW < SW - 1 and i + SW < SW * SH) / len(roi)
    try:
        for g, jpg in frames(proc, work, time.time() + args.max_wait + 8):
            n += 1
            if not aligned:
                # Auto-adapt: the median of the first 3 s (the head barely counts) vs the saved view.
                warm.append(g)
                if len(warm) < 6:
                    continue
                aligned = True
                cur = [sorted(f[i] for f in warm)[3] for i in range(SW * SH)]
                dx, dy, e0, e = estimate_shift(med, cur)
                suspect = e > SHIFT_SUSPECT and e0 > SHIFT_SUSPECT
                if suspect:
                    shift_note = f"view looks different (match error {e0}); checking the print is still there"
                if (dx, dy) != (0, 0) and e0 - e >= SHIFT_GAIN and max(abs(dx), abs(dy)) <= SHIFT_MAX:
                    keep_px = [(i % SW + dx, i // SW + dy) for i in mask]
                    mask = [y * SW + x for x, y in keep_px if 0 <= x < SW and 0 <= y < SH]
                    if len(mask) < 20:
                        moved_big, shift_note = True, f"print shifted out of view ({dx:+d},{dy:+d} px)"
                        break
                    xs, ys = [i % SW for i in mask], [i // SW for i in mask]
                    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
                    roi = [y * SW + x for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
                    med = shift_img(med, dx, dy)
                    write_pgm(sd / "median.pgm", med)
                    write_pgm(sd / "low.pgm", shift_img(C.read_pgm(sd / "low.pgm")[2], dx, dy))
                    st.update(roi=[x0, y0, x1, y1], mask=mask)
                    (sd / "roi.json").write_text(json.dumps(st))
                    shift_note = f"view shifted {dx:+d},{dy:+d} px (match error {e0} -> {e}): region followed it"
                continue
            clear = sum(g[i] < thr for i in mask) / len(mask)      # share of the print visible (dark) now
            motion = sum(abs(g[i] - prev[i]) for i in roi) / len(roi) if prev else 99
            glob_match.append(sum(abs(g[i] - med[i]) < 28 for i in range(0, SW * SH, 7)) / (SW * SH / 7))
            frozen = frozen + 1 if motion < 0.05 else 0       # identical frames = stream froze
            prev = g
            sharp = sharpness(g)
            if sharp > max_sharp:
                max_sharp = sharp
                if best and best[4] < 0.6 * max_sharp:          # current best is blurry next to this one
                    best = None
            if sharp < 0.6 * max_sharp or frozen >= 3:
                continue
            score = clear - motion / 200
            if best is None or score > best[0]:
                keep = work / f"best{jpg.suffix}"
                shutil.copy(jpg, keep)
                best = (score, clear, motion, keep, sharp)
            if clear >= args.clear and motion < 6:
                break
    finally:
        C.ff_stop(proc, pidf)
    if moved_big:
        shutil.rmtree(work, ignore_errors=True)
        sys.exit(f"CAMERA MOVED: {shift_note}. Re-run `print locate` + `print use`.")
    if best is None:
        shutil.rmtree(work, ignore_errors=True)
        sys.exit(f"no frames arrived. {C.ff_err(err)[-300:]}")
    _, clear, motion, keep, sharp = best
    if suspect and clear < 0.15:            # view differs AND the print isn't where it was: really moved
        moved_big = True
    if moved_big:
        shutil.rmtree(work, ignore_errors=True)
        sys.exit(f"CAMERA MOVED: {shift_note}; print visible {clear:.0%}. Re-run `print locate` + `print use`.")
    # reference = the sharpest best-frame any earlier shot of this camera/region produced (the locate
    # composite is itself soft, so it's a poor yardstick). Reset by `print use`.
    ref_file = sd / "sharp_ref.json"
    ref_sharp = json.loads(ref_file.read_text())["sharp"] if ref_file.exists() else sharp
    if sharp > ref_sharp or not ref_file.exists():
        ref_file.write_text(json.dumps({"sharp": round(max(sharp, ref_sharp), 2)}))
        ref_sharp = max(sharp, ref_sharp)
    fw, fh = C.img_size(keep)
    pad = 0.15
    bx0, by0 = x0 / SW, y0 / SH
    bw, bh = (x1 - x0 + 1) / SW, (y1 - y0 + 1) / SH
    cx0, cy0 = max(0.0, bx0 - bw * pad - 0.02), max(0.0, by0 - bh * pad - 0.02)
    cx1, cy1 = min(1.0, bx0 + bw * (1 + pad) + 0.02), min(1.0, by0 + bh * (1 + pad) + 0.02)
    cw = int((cx1 - cx0) * fw) // 2 * 2
    ch = int((cy1 - cy0) * fh) // 2 * 2
    outw = min(cw, args.width)                  # never upscale: native pixels are the real detail
    out = C.outdir(args) / f"{args.label + '-' if args.label else ''}print-{C.ts()}.jpg"
    C.run_ffmpeg(["-hide_banner", "-loglevel", "error", "-y", "-i", str(keep), "-vf",
                  f"crop={cw}:{ch}:{int(cx0 * fw)}:{int(cy0 * fh)},scale={outw}:-2", "-q:v", "2", str(out)],
                 camera=False)
    shutil.rmtree(work, ignore_errors=True)
    verdict = "print in view" if clear >= args.clear else f"best available, ~{1 - clear:.0%} of the print still hidden"
    # 15 Oct: the phone was moved mid-monitor and the whole-view check missed it (a bright scene still
    # "matches" a bright median). Almost none of the print visible in ANY frame = moved or print removed.
    moved = sum(glob_match) / len(glob_match) < 0.45 or clear < 0.10
    C.report(out, device, f"\n  {verdict}: {clear:.0%} of the print's pixels visible, "
                          f"motion {motion:.1f}, sharpness {sharp:.0f} (best seen {ref_sharp:.0f}), after {n} frames; "
                          f"crop {cw}x{ch} native px"
             + ("\n  WARNING: BLURRY, every frame was soft (autofocus hunting or a smudged lens)"
                if sharp < 0.5 * ref_sharp else "")
             + ("\n  WARNING: stream FROZE (identical frames). Phone locked or stalled?" if frozen >= 3 else "")
             + (f"\n  {shift_note}" if shift_note else "")
             + ("\n  WARNING: CAMERA MOVED or print removed: the print isn't where `print use` saved it. "
                "Re-aim, then re-run `print locate` + `print use`." if moved else ""))
    return "moved" if moved else "ok"


def cmd_monitor(args):
    """A print shot every args.every seconds. Each cycle waits up to 60% of `every` for the clearest
    frame, so mid-print shots are as unobstructed as the head allows. Same crop every time, so the
    numbered files line up as a time-lapse. One line per shot on stdout; images are NOT shown to Claude."""
    out = C.outdir(args)
    if getattr(args, "for_", None):
        args.max_min = args.for_ / 60
    end = time.time() + args.max_min * 60
    # search <=60% of the cycle (camera free the rest) and never more than 5 min, so a 60m interval
    # doesn't hold the camera for 36 min
    args.max_wait = max(20, min(args.every * 0.6, 300))
    base_label, k = args.label or "monitor", 0
    C.keep_awake("monitor")
    next_t = time.time()
    print(f"monitoring every {C.human(args.every)} for up to {C.human(args.max_min * 60)} "
          f"(<= {C.human(args.max_wait)} search per shot) -> {out}", flush=True)
    while time.time() < end:
        t0, k = time.time(), k + 1
        C.sleep_gap(next_t, args.every, k)
        next_t = t0 + args.every
        args.label = f"{base_label}-{k:03d}"
        for attempt in (1, 2):
            # a leftover capture from an earlier round holds the camera: clear it first
            subprocess.run(["pkill", "-9", "-f", "cam-shot-"], capture_output=True)
            try:
                status = cmd_shot(args)
            except SystemExit as e:      # stalled / camera lost: retry once after 10 s, then log and go on
                status = "moved" if str(e).startswith("CAMERA MOVED") else "failed"
                print(f"[{C.ts()}] shot {k} attempt {attempt}: {str(e)[:160]}", flush=True)
            if status == "moved":
                # Big move: shifting can't fix it, and telling print from logo/rods needs eyes. Re-locate
                # now (so the annotated image is ready), then STOP: the job ending notifies Claude, who
                # confirms with `print use` and restarts. Small bumps never get here: shot follows them.
                print(f"[{C.ts()}] camera moved: re-locating...", flush=True)
                args.label = f"{base_label}-relocate"
                try:
                    cmd_locate(args)
                except SystemExit as e:
                    print(f"[{C.ts()}] re-locate failed: {str(e)[:160]}", flush=True)
                print(f"[{C.ts()}] NEEDS CONFIRM: read the locate image above, run `print use <ids>`, "
                      f"then restart the monitor.", flush=True)
                sys.exit(3)
            if status == "ok":
                break
            if attempt == 1:
                time.sleep(10)
        sys.stdout.flush()
        time.sleep(max(0, args.every - (time.time() - t0)))
    print(f"[{C.ts()}] monitor finished after {k} shots", flush=True)


def cmd_print(args):
    {"locate": cmd_locate, "use": cmd_use, "shot": cmd_shot, "monitor": cmd_monitor}[args.action](args)
