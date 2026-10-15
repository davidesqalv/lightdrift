#!/usr/bin/env python3
"""
light_drift.py

Measure how a scanning light changes over time from raw captures of it.
Take one capture right after turning the light on, then more at intervals
(every 5 or 10 minutes, say) without touching the camera settings. The
script orders them by the capture time stored in each file and tracks:

  1. Overall level of each color channel
  2. Color balance (R/G and B/G ratios). These don't depend on exposure, so
     they are the cleanest sign that the light itself changed.
  3. Uniformity across the frame (center vs. corners)

It reads the raw sensor values directly: black level subtracted, no
demosaicing, no white balance, no camera profile. The numbers describe the
light, not any conversion.

Setup:
    pip install rawpy numpy matplotlib exifread

Usage:
    python light_drift.py                 (pick a subfolder of captures/ from a list)
    python light_drift.py captures/session1
    python light_drift.py DSC01234.ARW DSC01235.ARW DSC01236.ARW ...
    python light_drift.py captures/*.ARW
    python light_drift.py captures/*.ARW --grid 9 --gain 5 --out results
    python light_drift.py --film          (film scan mode, see below)

The earliest capture is the reference; every other capture is reported
relative to it. Two files work too.

Each run writes to a new timestamped folder inside light_drift_results (or
--out), e.g. light_drift_results/2026-10-09_14-30-05, so earlier results
are never overwritten.

To learn what counts as noise for your setup, include two captures taken back
to back at the start. Differences of that size don't mean anything.

Film scan mode (--film) works on scans of color negatives instead, to tell a
bad light apart from a bad exposure or a bad conversion. In every scan it
finds the clear film base around the frame, the bare light in the sprocket
holes and the black holder, none of which depend on the picture, and reports:

  1. Exposure: how much brighter or darker the shutter speed and ISO should
     make the film base, vs. how it actually came out. A gap between the two
     means the light (or the film) changed.
  2. Color of the film base, and of the light in the sprocket holes
  3. Headroom and clipping in each channel
  4. Evenness (film base at the middle of the edges vs. the corners) and stray
     light (light reaching the black holder)

    python light_drift.py --film captures/roll1
    python light_drift.py --film good.ARW bad.ARW --ref good.ARW

Run it on one roll at a time: different films have different base colors.
"""

import argparse
import csv
import os
import sys
import textwrap
from collections import Counter
from datetime import datetime
from fractions import Fraction

import numpy as np

CHANNELS = ("R", "G", "B")
PREVIEW_STEP = 8          # downsampling for the preview strip
RAW_EXTS = {".arw", ".cr2", ".cr3", ".nef", ".nrw", ".raf", ".orf", ".rw2", ".dng",
            ".pef", ".srw", ".3fr", ".iiq", ".erf", ".rwl", ".x3f", ".raw"}


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def choose_subfolder(parent="captures"):
    """List the subfolders of parent in the terminal and return the one picked by number."""
    if not os.path.isdir(parent):
        sys.exit(f"No '{parent}' folder here. Make one with a subfolder of raw files for each "
                 "session, or give the files on the command line.")
    folders = sorted(os.path.join(parent, d) for d in os.listdir(parent)
                     if os.path.isdir(os.path.join(parent, d)) and not d.startswith("."))
    if raw_files_in(parent):
        folders.insert(0, parent)
    if not folders:
        sys.exit(f"'{parent}' has no subfolders or raw files.")

    for i, f in enumerate(folders, 1):
        n = len(raw_files_in(f))
        print(f"  {i:2d}  {f}  ({n} raw file{'' if n == 1 else 's'})")
    while True:
        try:
            pick = input(f"Folder number (1-{len(folders)}): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit("No folder chosen.")
        if pick.isdigit() and 1 <= int(pick) <= len(folders):
            return folders[int(pick) - 1]
        print("  Not a valid number.")


def raw_files_in(folder):
    """Raw files directly inside folder, one per shot.

    When a shot is there as both the camera's raw and a DNG copy, the camera's
    raw is used. Skips the ._ files macOS leaves on camera cards.
    """
    by_shot = {}
    for f in sorted(os.listdir(folder)):
        stem, ext = os.path.splitext(f)
        if ext.lower() not in RAW_EXTS or f.startswith("."):
            continue
        if stem not in by_shot or by_shot[stem].lower().endswith(".dng"):
            by_shot[stem] = f
    return sorted(os.path.join(folder, f) for f in by_shot.values())


def load_planes(path):
    """Return ({'R','G','B': plane as fraction of clipping}, {channel: clipped fraction})."""
    import rawpy

    with rawpy.imread(path) as raw:
        data = raw.raw_image_visible.astype(np.float64)
        colors = raw.raw_colors_visible.copy()
        desc = raw.color_desc.decode("ascii")
        black = np.asarray(raw.black_level_per_channel, dtype=np.float64)
        white = float(raw.white_level)

    data -= black[colors]

    pieces, clipped = {}, {}
    for dy in (0, 1):
        for dx in (0, 1):
            ci = int(colors[dy, dx])
            name = desc[ci]
            plane = data[dy::2, dx::2]
            s = white - black[ci]
            pieces.setdefault(name, []).append(plane / s)
            clipped.setdefault(name, []).append(float(np.mean(plane >= 0.98 * s)))

    if any(c not in pieces for c in CHANNELS):
        sys.exit(f"{path}: sensor layout '{desc}' isn't a standard RGB Bayer pattern")

    h = min(p.shape[0] for ps in pieces.values() for p in ps)
    w = min(p.shape[1] for ps in pieces.values() for p in ps)
    planes = {c: np.mean([p[:h, :w] for p in pieces[c]], axis=0) for c in CHANNELS}
    clip = {c: max(clipped[c]) for c in CHANNELS}
    return planes, clip


def read_exif(path):
    """Shutter, aperture, ISO and capture time. Returns None if exifread isn't installed."""
    try:
        import exifread
    except ImportError:
        return None
    try:
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False)
    except Exception:
        return {"Shutter": "?", "Aperture": "?", "ISO": "?", "Program": "?", "time": None}

    info = {
        "Shutter": str(tags.get("EXIF ExposureTime", "?")),
        "Aperture": str(tags.get("EXIF FNumber", "?")),
        "ISO": str(tags.get("EXIF ISOSpeedRatings", "?")),
        "Program": str(tags.get("EXIF ExposureProgram", "?")),
        "time": None,
    }
    stamp = str(tags.get("EXIF DateTimeOriginal", tags.get("Image DateTime", "")))
    try:
        t = datetime.strptime(stamp.strip(), "%Y:%m:%d %H:%M:%S")
        sub = str(tags.get("EXIF SubSecTimeOriginal", "")).strip()
        if sub.isdigit():
            t = t.replace(microsecond=int(float("0." + sub) * 1e6))
        info["time"] = t
    except ValueError:
        pass
    return info


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

def luminance(planes):
    return (planes["R"] + 2 * planes["G"] + planes["B"]) / 4


def find_light_region(planes, inset):
    """Bounding box of the lit area (excludes the holder's dark border), inset by a margin."""
    lum = luminance(planes)
    thr = 0.5 * np.percentile(lum[::4, ::4], 99)
    mask = lum > thr
    rf, cf = mask.mean(axis=1), mask.mean(axis=0)
    rows = np.where(rf > 0.5 * rf.max())[0]
    cols = np.where(cf > 0.5 * cf.max())[0]
    h, w = lum.shape
    if len(rows) < 20 or len(cols) < 20:
        y0, y1, x0, x1 = 0, h, 0, w
    else:
        y0, y1, x0, x1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
    dy, dx = int((y1 - y0) * inset), int((x1 - x0) * inset)
    return y0 + dy, y1 - dy, x0 + dx, x1 - dx


def grid_medians(plane, box, ncols):
    y0, y1, x0, x1 = box
    p = plane[y0:y1, x0:x1]
    h, w = p.shape
    nrows = max(1, round(ncols * h / w))
    out = np.empty((nrows, ncols))
    for i, rs in enumerate(np.array_split(np.arange(h), nrows)):
        for j, cs in enumerate(np.array_split(np.arange(w), ncols)):
            out[i, j] = np.median(p[rs[0]:rs[-1] + 1, cs[0]:cs[-1] + 1])
    return out


def center_value(cells):
    r, c = cells.shape
    return cells[(r - 1) // 2:r // 2 + 1, (c - 1) // 2:c // 2 + 1].mean()


def corners_value(cells):
    return np.mean([cells[0, 0], cells[0, -1], cells[-1, 0], cells[-1, -1]])


def uniformity(cells):
    return {
        "min/max": 100 * cells.min() / cells.max(),
        "center/corners": 100 * center_value(cells) / corners_value(cells),
        "spread (CV)": 100 * cells.std() / cells.mean(),
    }


def measure(planes, box, ncols):
    y0, y1, x0, x1 = box
    m = {"cells": {}, "level": {}}
    for c in CHANNELS:
        m["cells"][c] = grid_medians(planes[c], box, ncols)
        m["level"][c] = float(np.median(planes[c][y0:y1, x0:x1]))
    m["cells"]["all"] = (m["cells"]["R"] + 2 * m["cells"]["G"] + m["cells"]["B"]) / 4
    m["level"]["all"] = (m["level"]["R"] + 2 * m["level"]["G"] + m["level"]["B"]) / 4
    for r, (n, d) in {"R/G": ("R", "G"), "B/G": ("B", "G")}.items():
        m["cells"][r] = m["cells"][n] / m["cells"][d]
        m["level"][r] = m["level"][n] / m["level"][d]
    return m


def pct(new, old):
    return 100 * (new / old - 1)


def summary_row(m, ref):
    """Numbers tracked over time, each relative to the reference capture."""
    row = {}
    for k in ("R", "G", "B", "all", "R/G", "B/G"):
        row[k] = pct(m["level"][k], ref["level"][k])
    ca, cr = m["cells"]["all"], ref["cells"]["all"]
    row["center"] = pct(center_value(ca), center_value(cr))
    row["corners"] = pct(corners_value(ca), corners_value(cr))
    row["ctr/crn"] = uniformity(ca)["center/corners"]
    row["CV"] = uniformity(ca)["spread (CV)"]
    return row


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def _status_lines(rows, width=100):
    """THE SHORT ANSWER: one row per check with its status word, the text wrapped under itself."""
    out = []
    for label, status, text in rows:
        lines = textwrap.wrap(text, width - 30) or [""]
        out.append(f"  {label:<18s}{status:<10s}{lines[0]}")
        out.extend(" " * 30 + line for line in lines[1:])
    return out


def _wrapped(text, indent="  ", width=100):
    return textwrap.wrap(text, width, initial_indent=indent, subsequent_indent=indent)


def _bullets(items, width=100):
    out = []
    for item in items:
        out += textwrap.wrap(item, width, initial_indent="  - ", subsequent_indent="    ")
    return out


def _when(t, i, have_times):
    return f"minute {t[i]:.0f}" if have_times else f"shot #{i}"


def brightness_story(t, a, have_times):
    """Plain words for a brightness series (% vs. the first shot).

    Returns (status, text, index where it settled or None, whether it jumps around).
    """
    a = np.asarray(a, dtype=float)
    span = a.max() - a.min()
    if span <= 2:
        return "OK", f"Steady: it moved less than {max(span, 0.1):.1f}% over the whole session.", None, False
    if len(a) == 2:
        return "CHANGED", f"{abs(a[1]):.1f}% {'brighter' if a[1] > 0 else 'dimmer'} in the second shot.", None, False
    steps = np.diff(a)
    jumps = steps[np.abs(steps) > 3]
    if len(jumps) >= 3 and (jumps > 0).any() and (jumps < 0).any():
        return "CHANGED", (f"Jumpy: it went up and down {len(jumps)} times, between {a.min():+.1f}% and "
                           f"{a.max():+.1f}%. over_time.png shows when."), None, True
    for s in range(1, len(a) - 1):
        rest = a[s:]
        if np.all(np.abs(rest - np.median(rest)) <= 1.5):
            return "CHANGED", (f"Got {abs(a[s]):.0f}% {'brighter' if a[s] > 0 else 'dimmer'} by "
                               f"{_when(t, s, have_times)}, then held steady (ended at {a[-1]:+.1f}%)."), s, False
    return "CHANGED", (f"Kept changing: {abs(a[-1]):.1f}% {'brighter' if a[-1] > 0 else 'dimmer'} by the last "
                       "shot, without settling."), None, False


def color_story(rg, bg):
    """Plain words for color series (R/G and B/G, % vs. the first shot): (status, text)."""
    rg, bg = np.asarray(rg, dtype=float), np.asarray(bg, dtype=float)
    r, b = rg[np.argmax(np.abs(rg))], bg[np.argmax(np.abs(bg))]
    big = max(abs(r), abs(b))
    if big < 1:
        return "OK", "Steady: it changed less than 1%."
    return ("SMALL" if big < COLOR_TOL else "CHANGED"), f"At most {_color_words(r, b, 1.0)} than the first shot."


def evenness_story(ctr):
    """Plain words for a center-vs-corners series (center as % of corners): (status, text)."""
    d = np.asarray(ctr, dtype=float) - 100
    level, change = d[0], float(np.abs(d - d[0]).max())
    if abs(level) < 1:
        text = "The middle and the corners are within 1% of each other"
    else:
        text = f"The middle is {abs(level):.0f}% {'brighter' if level > 0 else 'darker'} than the corners"
    text += ", and that didn't change." if change <= 2 else f", and that changed by up to {change:.1f} points."
    if abs(level) > 5:
        text += " That's uneven enough to show in scans (it includes the lens's own falloff toward the corners)."
    return ("OK" if abs(level) <= 5 and change <= 2 else "CHECK"), text


def build_report(caps, have_times):
    ref = caps[0]
    t = [c["minutes"] for c in caps]
    rows = [c["row"] for c in caps]
    when = f"over {t[-1]:.0f} minutes" if have_times else "in the order given (no capture times found)"
    L = ["LIGHT CHECK (bare light, no film)", "=" * 70,
         f"{len(caps)} shots {when}, each compared with the first one ({ref['name']}).", ""]

    b_status, b_text, settle, jumpy = brightness_story(t, [r["all"] for r in rows], have_times)
    c_status, c_text = color_story([r["R/G"] for r in rows], [r["B/G"] for r in rows])
    e_status, e_text = evenness_story([r["ctr/crn"] for r in rows])

    def settings(c):
        return tuple(c["exif"][k] for k in ("Shutter", "Aperture", "ISO"))

    differ = [i for i, c in enumerate(caps) if c["exif"] and ref["exif"] and settings(c) != settings(ref)]
    if not ref["exif"]:
        s_row = ("Same settings", "?", "Couldn't read the camera settings (pip install exifread).")
    elif differ:
        s_row = ("Same settings", "CHECK", f"Shots {', '.join('#' + str(i) for i in differ)} used different "
                 "settings, so their brightness can't be compared (color and evenness still can).")
    else:
        s_row = ("Same settings", "OK", "Same shutter speed, aperture and ISO in every shot.")
    clipped = [i for i, c in enumerate(caps) if max(c["clip"].values()) > 0.001]
    if clipped:
        k_row = ("Blown out", "FIX", f"Shots {', '.join('#' + str(i) for i in clipped)} have pixels at the "
                 "sensor's maximum, which can't be measured. Use a faster shutter speed.")
    else:
        k_row = ("Blown out", "OK", "Nothing blown out.")

    L.append("THE SHORT ANSWER")
    L += _status_lines([("Brightness", b_status, b_text), ("Color", c_status, c_text),
                        ("Evenness", e_status, e_text), s_row, k_row])
    L.append("")
    if jumpy:
        bottom = ("The light's brightness jumps around. Check what happened at those times (switched off and "
                  "on? charger plugged in?) in over_time.png.")
    elif settle is not None:
        if settle == 1:     # settled by the very next shot, so it may well have settled sooner
            when = f"by minute {t[1]:.0f}" if have_times else "by the next shot"
            bottom = (f"Let the light warm up before scanning. It had settled {when}, the first shot after the "
                      "start, so it may need less. Shoot every minute or two at the start to find out.")
        else:
            wait = f"about {t[settle]:.0f} minutes" if have_times else f"until shot #{settle}"
            bottom = f"Let the light warm up {wait} before scanning. After that it's steady."
    elif b_status != "OK":
        bottom = "The light kept changing through the whole session, so early and late scans will differ."
    else:
        bottom = "The light's brightness is steady."
    if c_status == "CHANGED":
        bottom += " Its color shifts too, enough to tint scans slightly."
    if e_status == "CHECK":
        bottom += " It's not very even across the frame."
    L += _wrapped("Bottom line: " + bottom)
    L.append("")

    L.append("HOW IT WORKS (roughly)")
    L += _wrapped("You shot the bare light (no film) a few times. For each shot the script reads the raw sensor "
                  "data straight from the file, before any conversion, and compares it with the first shot. Only "
                  "the lit area counts, not the dark edges around it.")
    L += _bullets([
        "Brightness: the average level of the light. +5% means 5% more light than in the first shot.",
        "Color: red compared with green, and blue compared with green. Exposure doesn't change these, so if "
        "they move, the light's color really changed. \"Bluer\" means more blue compared with green.",
        "Evenness: the lit area is cut into a grid of patches, and the middle patch is compared with the four "
        "corner patches.",
        "Blown out: pixels at the sensor's maximum. Their real brightness is unknown.",
        "Two shots taken back to back show how much the numbers wobble on their own. Ignore changes that small."])
    L.append("")

    L.append("SHOT BY SHOT (% change vs. the first shot)")
    head = (f"  {'#':>3s}  {'file':14s} {'min':>6s} | {'brightness':>10s} {'bluer':>6s} {'redder':>6s} | "
            f"{'middle vs':>9s} | note")
    L.append(head)
    L.append(f"  {'':>3s}  {'':14s} {'':>6s} | {'':>10s} {'':>6s} {'':>6s} | {'corners':>9s} |")
    L.append("  " + "-" * (len(head) - 2))
    for i, (c, r) in enumerate(zip(caps, rows)):
        tm = f"{c['minutes']:6.1f}" if have_times else f"{'#' + str(i):>6s}"
        note = ", ".join(n for n, on in (("settings differ", i in differ), ("blown out", i in clipped)) if on)
        L.append(f"  {i:3d}  {c['name'][:14]:14s} {tm} | {r['all']:+10.1f} {r['B/G']:+6.1f} {r['R/G']:+6.1f} | "
                 f"{r['ctr/crn'] - 100:+8.1f}% | {note}".rstrip())
    L.append("")
    L.append("  middle vs corners: how much brighter the middle of the frame is than the corners (0 = even)")
    L.append("")

    L.append("FILES")
    L.append("  over_time.png      brightness, color and evenness, shot by shot")
    L.append("  previews.png       every shot, with the differences exaggerated so you can see them")
    L.append("  level_change.png   first vs. last shot: where it got brighter or dimmer")
    L.append("  color_shift.png    first vs. last shot: where the color changed")
    L.append("  uniformity.png     how even the light is across the frame, first vs. last shot")
    L.append("  results.csv        all the numbers")
    return "\n".join(L)


def write_csv(path, caps, have_times):
    keys = ("R", "G", "B", "all", "R/G", "B/G", "center", "corners", "ctr/crn", "CV")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["index", "file", "capture_time", "minutes"] + [f"{k} %" if k not in ("ctr/crn", "CV") else k for k in keys])
        for i, c in enumerate(caps):
            t = c["time"].isoformat(sep=" ") if c["time"] else ""
            mins = f"{c['minutes']:.2f}" if have_times else ""
            w.writerow([i, c["name"], t, mins] + [f"{c['row'][k]:.4f}" for k in keys])


# --------------------------------------------------------------------------
# Figures
# --------------------------------------------------------------------------

def _heatmap(ax, data, title, vlim):
    im = ax.imshow(data, cmap="RdBu_r", vmin=-vlim, vmax=vlim)
    ax.set_title(title, fontsize=10)
    ax.set_xticks([])
    ax.set_yticks([])
    if data.size <= 150:
        for (i, j), v in np.ndenumerate(data):
            ax.text(j, i, f"{round(v, 1) + 0.0:+.1f}", ha="center", va="center", fontsize=7,
                    color="white" if abs(v) > 0.6 * vlim else "black")
    return im


def _vlim(*arrays):
    m = max(float(np.abs(a).max()) for a in arrays)
    return max(1.0, np.ceil(m * 2) / 2)


def _explain(ax, question, how):
    """Panel title as a plain question, with a line underneath on how it's measured."""
    ax.set_title(question, loc="left", fontsize=11, fontweight="bold", pad=10 + 11 * (how.count("\n") + 1))
    ax.text(0, 1.012, how, transform=ax.transAxes, fontsize=8.5, color="#555555", va="bottom")


def _band(ax, lo, hi, label):
    ax.axhspan(lo, hi, color="#2ca02c", alpha=0.12, lw=0, label=label)


def figure_over_time(outdir, caps, have_times, plt):
    x = [c["minutes"] for c in caps] if have_times else list(range(len(caps)))
    xlabel = "minutes since the first shot" if have_times else "shot number"
    rows = [c["row"] for c in caps]
    col = {"R": "#d62728", "G": "#2ca02c", "B": "#1f77b4"}

    fig, axes = plt.subplots(3, 1, figsize=(9, 11), sharex=True, constrained_layout=True)
    ax = axes[0]
    _band(ax, -1, 1, "within 1%: too small to matter")
    for k in CHANNELS:
        ax.plot(x, [r[k] for r in rows], color=col[k], lw=0.8, alpha=0.5, label=f"{k} channel")
    ax.plot(x, [r["all"] for r in rows], "o-", color="#222222", lw=2, ms=4, label="overall")
    _explain(ax, "Did the light get brighter or dimmer?", "% change in brightness vs. the first shot.")

    ax = axes[1]
    _band(ax, -1, 1, "within 1%: too small to matter")
    ax.plot(x, [r["B/G"] for r in rows], "o-", color=col["B"], ms=4, label="bluer (+) or less blue (-)")
    ax.plot(x, [r["R/G"] for r in rows], "o-", color=col["R"], ms=4, label="redder (+) or less red (-)")
    _explain(ax, "Did the light's color change?",
             "Blue and red compared with green, % change vs. the first shot. Exposure doesn't affect these.")

    ax = axes[2]
    mid = [r["ctr/crn"] - 100 for r in rows]
    ax.plot(x, mid, "o-", color="#444444", ms=4, label="middle vs. corners")
    ax.set_ylim(min(min(mid) - 1, -2), max(max(mid) + 1, 6))     # so a flat line looks flat
    _explain(ax, "Is the middle brighter than the corners?",
             "% brighter in the middle of the frame than in the corners. 0 = perfectly even.\n"
             "A flat line = it didn't change.")
    ax.set_xlabel(xlabel)

    for ax in axes:
        ax.axhline(0, color="black", lw=0.6)
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8, loc="best", frameon=False)
        ax.set_ylabel("%")
    fig.savefig(os.path.join(outdir, "over_time.png"), dpi=130)
    plt.close(fig)


def figure_previews(outdir, caps, ref_levels, box, gain, have_times, plt):
    from matplotlib.patches import Rectangle
    n = len(caps)
    ncol = min(n, 4)
    nrow = int(np.ceil(n / ncol))
    fig, axes = plt.subplots(nrow, ncol, figsize=(4.2 * ncol, 3.2 * nrow + 0.6),
                             squeeze=False, constrained_layout=True)
    y0, y1, x0, x1 = (v / PREVIEW_STEP for v in box)
    for k, ax in enumerate(axes.flat):
        ax.set_xticks([])
        ax.set_yticks([])
        if k >= n:
            ax.axis("off")
            continue
        c = caps[k]
        rgb = np.dstack([c["preview"][ch] / ref_levels[ch] for ch in CHANNELS])
        ax.imshow(np.clip(0.5 + (rgb - 1.0) * gain, 0, 1))
        ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, fill=False, ec="yellow", lw=0.8, ls="--"))
        t = f"{c['minutes']:.1f} min" if have_times else f"#{k}"
        ax.set_title(f"{t}  ({c['name']})", fontsize=9)
    fig.suptitle(f"Every shot, with its differences from the first shot exaggerated {gain:g}x.\n"
                 "Plain grey = same as the first shot. Lighter or darker = brightness changed. A tint = color "
                 "changed. Yellow box = the area measured.", fontsize=10)
    fig.savefig(os.path.join(outdir, "previews.png"), dpi=110)
    plt.close(fig)


def figures_first_last(outdir, ma, mb, plt):
    names = {"R": "Red", "G": "Green", "B": "Blue", "all": "Overall", "R/G": "Red vs. green", "B/G": "Blue vs. green"}

    keys = ("R", "G", "B", "all")
    maps = [pct(mb["cells"][k], ma["cells"][k]) for k in keys]
    v = _vlim(*maps)
    fig, axes = plt.subplots(1, 4, figsize=(16, 3.8), constrained_layout=True)
    for ax, k, d in zip(axes, keys, maps):
        im = _heatmap(ax, d, names[k], v)
    fig.colorbar(im, ax=axes, shrink=0.8, label="% brighter (+) or dimmer (-)")
    fig.suptitle("Where did the light get brighter or dimmer? Last shot vs. first, % change in each patch of the frame")
    fig.savefig(os.path.join(outdir, "level_change.png"), dpi=130)
    plt.close(fig)

    keys = ("R/G", "B/G")
    maps = [pct(mb["cells"][k], ma["cells"][k]) for k in keys]
    v = _vlim(*maps)
    fig, axes = plt.subplots(1, 2, figsize=(10, 4), constrained_layout=True)
    for ax, k, d in zip(axes, keys, maps):
        im = _heatmap(ax, d, names[k], v)
    fig.colorbar(im, ax=axes, shrink=0.8, label="% more (+) or less (-), compared with green")
    fig.suptitle("Where did the light's color change? Last shot vs. first, % change in each patch")
    fig.savefig(os.path.join(outdir, "color_shift.png"), dpi=130)
    plt.close(fig)

    ca, cb = ma["cells"]["all"], mb["cells"]["all"]
    ua, ub = pct(ca, ca.mean()), pct(cb, cb.mean())
    shape = pct(cb / cb.mean(), ca / ca.mean())
    fig, axes = plt.subplots(1, 3, figsize=(14, 4), constrained_layout=True)
    v = _vlim(ua, ub)
    _heatmap(axes[0], ua, "First shot", v)
    im = _heatmap(axes[1], ub, "Last shot", v)
    fig.colorbar(im, ax=axes[:2], shrink=0.8, label="% brighter (+) or dimmer (-) than average")
    im2 = _heatmap(axes[2], shape, "What changed (overall brightening\nor dimming taken out)", _vlim(shape))
    fig.colorbar(im2, ax=axes[2], shrink=0.8, label="% points")
    fig.suptitle("How even is the light? Each patch compared with the frame's average")
    fig.savefig(os.path.join(outdir, "uniformity.png"), dpi=130)
    plt.close(fig)


# --------------------------------------------------------------------------
# Film scan mode
# --------------------------------------------------------------------------
# Each scan is cut into small blocks, sorted into:
#   film base   the clear film between frames and along the edges. On a negative
#               it's the thinnest film, so the brightest, and every frame of a roll
#               has the same one, so its brightness and color depend only on the
#               light and the exposure, not on the picture.
#   bare light  sprocket holes and gaps where light gets past the film
#   holder      the black mask around the film. Any light there is stray light.
#   film        everything else

FILM_BLOCK = 16             # block size, in per-channel pixels
FILM_EDGE = 0.08            # how far in from each side to look for the film base
FILM_PREVIEW_STEP = 12      # downsampling for film_check.png
FILM_CHECK_MAX = 24         # most scans shown in film_check.png
CLIP_LEVEL = 0.98           # fraction of the white level counted as clipped
EXPOSURE_DIFF = 0.5         # stops brighter or darker than the reference worth noting
EXPOSURE_TOL = 0.15         # stops the settings don't explain, manual exposure
EXPOSURE_TOL_AUTO = 0.35    # same with auto exposure, which records rounded shutter speeds
COLOR_TOL = 2.0             # % change in film base or light color
UNDER_STOPS = 1.5           # film base this far below clipping counts as underexposed
NOISE_STOPS = 9.0           # densest film this far below clipping is in the sensor noise
EVEN_TOL = 5.0              # % points change in mid/corner (scan-to-scan noise is about 1-1.5)
STRAY_TOL = 0.5             # % points change in stray light
NEGATIVE_RB = 1.2           # a film base with R/B below this has no orange mask
SHUTTER_SPEEDS = ("1/8000", "1/6400", "1/5000", "1/4000", "1/3200", "1/2500", "1/2000", "1/1600", "1/1250",
                  "1/1000", "1/800", "1/640", "1/500", "1/400", "1/320", "1/250", "1/200", "1/160", "1/125",
                  "1/100", "1/80", "1/60", "1/50", "1/40", "1/30", "1/25", "1/20", "1/15", "1/13", "1/10",
                  "1/8", "1/6", "1/5", "1/4", "0.3", "0.4", "0.5", "0.6", "0.8", "1", "1.3", "1.6", "2")


def _number(s):
    """'1/125', '5.6' or '100' as a float. None if missing or zero."""
    try:
        v = float(Fraction(str(s).strip()))
    except (ValueError, ZeroDivisionError):
        return None
    return v if v > 0 else None


def is_auto_exposure(exif):
    return not exif or exif.get("Program") != "Manual"


def settings_stops(caps):
    """Exposure each scan's shutter, ISO and aperture give, in stops. None if shutter or ISO are missing."""
    t, iso, n = ([_number((c["exif"] or {}).get(k)) for c in caps] for k in ("Shutter", "ISO", "Aperture"))
    if None in t or None in iso:
        return None, False
    use_n = None not in n
    return [float(np.log2(ti * si / (ni * ni if use_n else 1.0))) for ti, si, ni in zip(t, iso, n)], use_n


def parse_box(s):
    try:
        x0, y0, x1, y1 = (float(v) for v in s.split(","))
    except ValueError:
        sys.exit("--base takes left,top,right,bottom as fractions of the frame, e.g. 0.01,0.3,0.04,0.7")
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        sys.exit("--base box has to be inside the frame: 0 <= left < right <= 1 and 0 <= top < bottom <= 1")
    return x0, y0, x1, y1


def film_blocks(planes):
    """Mean level and clipped fraction of each channel in each block."""
    b = FILM_BLOCK
    hb, wb = planes["R"].shape[0] // b, planes["R"].shape[1] // b
    split = {c: planes[c][:hb * b, :wb * b].reshape(hb, b, wb, b) for c in CHANNELS}
    means = {c: split[c].mean(axis=(1, 3)) for c in CHANNELS}
    clipped = {c: (split[c] >= CLIP_LEVEL).mean(axis=(1, 3)) for c in CHANNELS}
    return means, clipped


def _outer_ring(ok, n):
    """The first n ok blocks walking in from each side of the frame, along every row and column."""
    ring = np.zeros_like(ok)
    for axis in (0, 1):
        for flip in (False, True):
            o = np.flip(ok, axis) if flip else ok
            first = o & (np.cumsum(o, axis=axis) <= n)
            ring |= np.flip(first, axis) if flip else first
    return ring


def _grow(mask):
    """mask plus its eight neighbours."""
    p = np.pad(mask, 1)
    h, w = mask.shape
    return np.any([p[dy:dy + h, dx:dx + w] for dy in range(3) for dx in range(3)], axis=0)


def _touching_edge(mask):
    """The parts of mask connected to the outside edge of the frame."""
    out = np.zeros_like(mask)
    out[0], out[-1], out[:, 0], out[:, -1] = mask[0], mask[-1], mask[:, 0], mask[:, -1]
    while True:
        grown = _grow(out) & mask
        if (grown == out).all():
            return out
        out = grown


def find_film_parts(means, base_box):
    """Block masks for the film base, bare light and holder, plus log2 brightness of every block.

    On a negative the film base is the thinnest film, so the brightest film
    that's orange (bare light isn't). The holder is the dark area connected to
    the outside of the frame. The film base is then taken from the strip of
    film between the holder and the picture: the first few blocks in from each
    side of the film, past the holder and sprocket holes, at the film base's
    level and color. Thin parts of the picture can be as bright, but they sit
    further in.
    """
    hb, wb = means["R"].shape
    lum = (means["R"] + 2 * means["G"] + means["B"]) / 4
    L = np.log2(np.maximum(lum, 1e-6))
    yy, xx = np.mgrid[0:hb, 0:wb]
    orange = means["R"] >= NEGATIVE_RB * means["B"]
    if orange.mean() < 0.05:             # not a color negative; measure it anyway
        orange = np.ones_like(orange)
    # Bright blocks that aren't orange are bare light; next to them is a blur of light and film
    near_bare = _grow(_grow(~orange & (L > np.median(L))))
    film_like = orange & ~near_bare
    if not film_like.any():
        film_like = orange

    rough = np.percentile(L[film_like], 99)
    holder = _touching_edge(L < rough - 4.5)

    # The film's own edges: rows and columns that are mostly not holder
    rows = np.where((~holder).mean(axis=1) > 0.2)[0]
    cols = np.where((~holder).mean(axis=0) > 0.2)[0]
    y0, y1, x0, x1 = (rows[0], rows[-1] + 1, cols[0], cols[-1] + 1) if len(rows) > 10 and len(cols) > 10 \
        else (0, hb, 0, wb)
    ey, ex = FILM_EDGE * (y1 - y0), FILM_EDGE * (x1 - x0)
    inside = (yy >= y0) & (yy < y1) & (xx >= x0) & (xx < x1)
    border = inside & ((yy < y0 + ey) | (yy >= y1 - ey) | (xx < x0 + ex) | (xx >= x1 - ex))

    if base_box:
        bx0, by0, bx1, by1 = base_box
        base = (xx >= bx0 * wb) & (xx < bx1 * wb) & (yy >= by0 * hb) & (yy < by1 * hb)
        if not base.any():
            sys.exit("--base box is too small; make it at least a few percent of the frame across.")
        even = base
    else:
        cand = border & film_like & ~holder
        seed = np.percentile(L[cand], 95) if cand.any() else rough
        # Only the outermost film: skip holder (much darker) and bare light (brighter, not orange)
        ok = ~holder & film_like & (np.abs(L - seed) < 0.5)
        ring = border & _outer_ring(ok, 4)
        for fallback in (cand, border):
            if not ring.any():
                ring = fallback
        seed = np.median(L[ring])
        base = ring & (np.abs(L - seed) <= 0.1)
        if base.sum() < 4:
            base = ring
        rg = means["R"] / np.maximum(means["G"], 1e-9)
        bg = means["B"] / np.maximum(means["G"], 1e-9)
        same_color = (np.abs(rg / np.median(rg[base]) - 1) < 0.03) & (np.abs(bg / np.median(bg[base]) - 1) < 0.03)
        # For evenness, allow a wider brightness range, or a strong hotspot would hide itself
        even = ring & same_color & (np.abs(L - seed) <= 0.5)
        base &= same_color

    base_L = np.median(L[base])
    bare = L > base_L + 0.4
    holder = _touching_edge(L < base_L - 4.5)
    return base, even, bare, holder, L, (y0, y1, x0, x1)


def hole_light_rb(planes, bare, base_lum):
    """Red/blue balance of the bare light in the sprocket holes, or None if too little shows.

    Green is usually clipped there, so only red and blue are used, from the
    brightest pixels well above the film base where neither is clipped.
    """
    if bare.sum() < 4:
        return None
    b = FILM_BLOCK
    hb, wb = bare.shape
    r, g, bl = (planes[c][:hb * b, :wb * b] for c in CHANNELS)
    lum = (r + 2 * g + bl) / 4
    mask = np.repeat(np.repeat(bare, b, axis=0), b, axis=1)
    mask &= (lum > 3 * base_lum) & (r < CLIP_LEVEL) & (bl < CLIP_LEVEL)
    if mask.sum() < 500:
        return None
    top = mask & (lum >= np.percentile(lum[mask], 80))
    return float(np.median(r[top]) / np.median(bl[top]))


def measure_film(planes, base_box):
    means, clipped = film_blocks(planes)
    base, even, bare, holder, L, (y0, y1, x0, x1) = find_film_parts(means, base_box)
    film = ~_grow(bare) & ~holder     # the blurred edges of sprocket holes aren't film either
    lum = 2.0 ** L

    bb = {c: float(np.median(means[c][base])) for c in CHANNELS}
    bb["all"] = (bb["R"] + 2 * bb["G"] + bb["B"]) / 4
    f = {"base": bb, "R/G": bb["R"] / bb["G"], "B/G": bb["B"] / bb["G"]}
    f["negative"] = bb["R"] / max(bb["B"], 1e-9) >= NEGATIVE_RB
    f["headroom"] = {c: float(-np.log2(max(bb[c], 1e-6))) for c in CHANNELS}
    f["densest"] = {c: float(-np.log2(max(np.percentile(means[c][film], 0.5), 1e-6))) for c in CHANNELS}
    f["base_clip"] = {c: 100 * float(clipped[c][base].mean()) for c in CHANNELS}
    f["film_clip"] = {c: 100 * float(clipped[c][film].mean()) for c in CHANNELS}
    f["light R/B"] = hole_light_rb(planes, bare, bb["all"])
    f["bare %"] = 100 * float(bare.mean())
    # Stray light: the black holder's brightness as % of the average light through the film
    f["stray"] = 100 * float(np.median(lum[holder]) / lum[film].mean()) if holder.mean() > 0.005 else None

    # Evenness: how much brighter the clear film is at the middle of the film's edges than at its corners.
    # A straight line through brightness vs. distance from the center (squared), fitted over all the clear
    # film along the edges, is steadier than comparing a few corner blocks. r squared is 0.5 at the middle
    # of an edge and 1 in a corner.
    yy, xx = np.mgrid[0:base.shape[0], 0:base.shape[1]]
    r2 = (np.hypot((xx + 0.5 - x0) / (x1 - x0) - 0.5, (yy + 0.5 - y0) / (y1 - y0) - 0.5) / np.hypot(0.5, 0.5)) ** 2
    if even.sum() >= 30 and np.ptp(r2[even]) >= 0.2:
        slope = np.polyfit(r2[even], L[even], 1)[0]
        f["mid/corner"] = 100 * (2 ** (-0.5 * slope) - 1)
    else:
        f["mid/corner"] = None

    any_clip = np.max([clipped[c] for c in CHANNELS], axis=0) > 0
    f["masks"] = {"base": base, "bare": bare, "holder": holder, "clipped": film & any_clip}
    return f


def film_rows(caps, ri, stops):
    """Numbers compared against the reference scan."""
    rf = caps[ri]["f"]
    for i, c in enumerate(caps):
        f = c["f"]
        row = {"set": stops[i] - stops[ri] if stops else None,
               "measured": float(np.log2(f["base"]["all"] / rf["base"]["all"])),
               "R/G": pct(f["R/G"], rf["R/G"]),
               "B/G": pct(f["B/G"], rf["B/G"])}
        row["unexplained"] = row["measured"] - row["set"] if stops else None
        row["light"] = pct(f["light R/B"], rf["light R/B"]) if f["light R/B"] and rf["light R/B"] else None
        c["row"] = row


def _color_words(rg, bg, least=0.5):
    """'2.6% bluer and 0.7% less red' for changes in B/G and R/G, in %."""
    parts = []
    if abs(bg) >= least:
        parts.append(f"{abs(bg):.1f}% {'bluer' if bg > 0 else 'less blue'}")
    if abs(rg) >= least:
        parts.append(f"{abs(rg):.1f}% {'redder' if rg > 0 else 'less red'}")
    return " and ".join(parts) or f"under {least:g}%"


def suggest_exposure(negs):
    """Shutter speed that puts the clear film's brightest channel half a stop under blown out, at the usual ISO."""
    target = []
    for c in negs:
        ex = c["exif"] or {}
        t, iso = _number(ex.get("Shutter")), _number(ex.get("ISO"))
        if t and iso:
            target.append(t * iso * 2 ** (min(c["f"]["headroom"].values()) - 0.5))
    if not target:
        return None
    iso = Counter((c["exif"] or {}).get("ISO") for c in negs).most_common(1)[0][0]
    t = float(np.median(target)) / _number(iso)
    best = min(SHUTTER_SPEEDS, key=lambda s: abs(np.log2(_number(s) / t)))
    return f"{best} s at ISO {iso}"


def film_findings(caps, ri):
    """For each scan, a list of (kind, tag, text): what's worth a look, in plain words.

    kind is 'light' (the light or stray light changed), 'color' (the clear film
    changed color but the light in the sprocket holes didn't: the film, or the
    light's green), 'exposure' or 'other'. tag is a word or two for the tables.
    """
    rf = caps[ri]["f"]
    out = []
    for i, c in enumerate(caps):
        f, row, notes = c["f"], c["row"], []
        out.append(notes)
        if not f["negative"]:
            notes.append(("other", "skipped", "Not a color negative (no orange film base): a slide, black and white "
                                              "film, or no film. Left out."))
            continue

        clipped = [ch for ch in CHANNELS if f["base_clip"][ch] > 1]
        if clipped:
            notes.append(("exposure", "blown out", f"The clear film is blown out in {'/'.join(clipped)}. The converter "
                                                   "can't see its real color, which usually gives a strong color "
                                                   "cast. Expose darker."))
        else:
            spots = [f"{f['film_clip'][ch]:.2f}% in {ch}" for ch in CHANNELS if f["film_clip"][ch] > 0.1]
            if spots:
                notes.append(("exposure", "blown out", f"Parts of the film are blown out ({', '.join(spots)}). "
                                                       "Those spots lose their color."))
        if i == ri:
            continue

        # Noise on its own goes in the short answer; here only when it's clearly worse than the reference
        deep = max(CHANNELS, key=lambda ch: f["densest"][ch])
        extra = f["densest"][deep] - rf["densest"][deep]
        if f["densest"][deep] > NOISE_STOPS and extra > 1:
            notes.append(("exposure", "noisy", f"Its densest film is {extra:.1f} stops darker than in the reference, "
                                               "down in the sensor noise. That's the brightest part of the picture "
                                               "once converted, so expect noise and off colors there."))

        auto = is_auto_exposure(c["exif"]) or is_auto_exposure(caps[ri]["exif"])
        tol = EXPOSURE_TOL_AUTO if auto else EXPOSURE_TOL
        m, u = row["measured"], row["unexplained"]
        if u is not None and abs(u) > tol:
            notes.append(("light", "brightness", f"It came out {abs(u):.2f} stops {'brighter' if u > 0 else 'darker'} "
                                                 "than its shutter speed and ISO explain. The light got "
                                                 f"{'brighter' if u > 0 else 'dimmer'}, or it's a different film."))
        elif abs(m) > EXPOSURE_DIFF:
            ex, rex = c["exif"] or {}, caps[ri]["exif"] or {}
            changed = [f"{'' if k == 'Shutter' else k + ' '}{ex.get(k, '?')} instead of {rex.get(k, '?')}"
                       for k in ("Shutter", "ISO", "Aperture") if ex.get(k) != rex.get(k)]
            why = (f"because the camera used {' and '.join(changed)}. Not the light." if u is not None and changed
                   else "and the camera settings aren't in the file.")
            notes.append(("exposure", "exposure", f"It's {abs(m):.1f} stops {'brighter' if m > 0 else 'darker'} than "
                                                  f"the reference, {why}"))

        dl = row["light"]
        words = _color_words(row["R/G"], row["B/G"])
        if max(abs(row["R/G"]), abs(row["B/G"])) > COLOR_TOL:
            if dl is None:
                notes.append(("light", "film color", f"The clear film changed color ({words}). Either the light's "
                                                     "color changed or it's a different film; there are no sprocket "
                                                     "holes to tell which."))
            elif abs(dl) > COLOR_TOL:
                notes.append(("light", "light color", f"The light's color changed: the sprocket holes are "
                                                      f"{abs(dl):.1f}% {'warmer' if dl > 0 else 'cooler'}, and the "
                                                      f"clear film changed with them ({words})."))
            else:
                notes.append(("color", "film color", f"The clear film changed color ({words}) but the light in the "
                                                     "sprocket holes didn't. Either it's a different film or strip, or "
                                                     "the light's green shifted (green is blown out in the sprocket "
                                                     "holes, so it can't show there). The second often happens right "
                                                     "after the light is switched on."))
        elif dl is not None and abs(dl) > COLOR_TOL:
            notes.append(("light", "light color", f"The light in the sprocket holes got {abs(dl):.1f}% "
                                                  f"{'warmer' if dl > 0 else 'cooler'} but the clear film didn't. "
                                                  "Check the sprocket holes in film_check.png; something may be "
                                                  "partly covering them."))

        if f["mid/corner"] is not None and rf["mid/corner"] is not None:
            d = f["mid/corner"] - rf["mid/corner"]
            if abs(d) > EVEN_TOL:
                notes.append(("light", "evenness", f"The middle of the frame is lit {'more' if d > 0 else 'less'} "
                                                   f"strongly than in the reference: the clear film is "
                                                   f"{f['mid/corner']:+.0f}% brighter mid-edge than at the corners, "
                                                   f"against {rf['mid/corner']:+.0f}%. The light, or stray light, "
                                                   "changed."))
        if f["stray"] is not None and rf["stray"] is not None and f["stray"] - rf["stray"] > STRAY_TOL:
            notes.append(("light", "stray light", f"More stray light: {f['stray']:.1f}% of the light lands on the "
                                                  f"black holder, against {rf['stray']:.1f}% in the reference. Light "
                                                  "bouncing around adds a haze, strongest in the middle."))
    return out


def film_verdict(caps, ri, findings, stops):
    """THE SHORT ANSWER rows (label, status, text), the bottom line, and what to do."""
    negs = [c for c in caps if c["f"]["negative"]]
    if not negs:
        return ([("Color negatives", "?", "None of the scans look like color negatives, so there's nothing to "
                                          "check. This mode is for color negative film.")], "Nothing to check.", [])
    others = [c for c in negs if c is not caps[ri]]

    def flagged(tag):
        return [c["name"] for c, notes in zip(caps, findings) if any(t == tag for _, t, _ in notes)]

    rows, todo = [], []

    bad = flagged("brightness")
    if not stops:
        rows.append(("Light brightness", "?", "Can't tell: shutter speed or ISO is missing from the files."))
    elif bad:
        rows.append(("Light brightness", "CHECK", "Some scans came out brighter or darker than their camera "
                                                  f"settings explain: {_names(bad)}."))
    elif others:
        u = max(abs(c["row"]["unexplained"]) for c in others)
        rows.append(("Light brightness", "OK", "Steady. Every scan came out as bright as its camera settings say it "
                                               f"should (within {u:.2f} stops)."))
    else:
        rows.append(("Light brightness", "?", "Needs at least two color negatives to compare."))

    light = [c["f"]["light R/B"] for c in negs if c["f"]["light R/B"]]
    bad = flagged("light color")
    if bad:
        rows.append(("Light color", "CHECK", f"Changed in {_names(bad)}."))
    elif len(light) >= 2:
        rows.append(("Light color", "OK", "Steady. The light in the sprocket holes changed color by "
                                          f"{pct(max(light), min(light)):.1f}% at most."))
    else:
        rows.append(("Light color", "?", "Can't check directly: no sprocket holes in view, or they're blown out."))

    bad = flagged("film color")
    shift = max((max(abs(c["row"]["R/G"]), abs(c["row"]["B/G"])) for c in caps if c["name"] in bad), default=0)
    if bad:
        rows.append(("Film base color", "CHECK", f"Shifted by up to {shift:.1f}% in {_names(bad)}. A different "
                                                 "film, or the light warming up."))
    else:
        rows.append(("Film base color", "OK", f"The same in every scan (within {COLOR_TOL:g}%)."))

    meas = [c["row"]["measured"] for c in negs]
    spread = max(meas) - min(meas)
    auto = sorted({(c["exif"] or {}).get("Program", "?") for c in negs if is_auto_exposure(c["exif"])})
    if spread > 0.3 and auto:
        rows.append(("Same exposure?", "FIX", f"No. The camera picked the exposure itself ({', '.join(auto)}), so "
                                              f"the clear film came out up to {spread:.1f} stops brighter in some "
                                              "scans than in others."))
        todo.append("Put the camera in manual (M) and keep the same shutter speed and ISO for the whole roll.")
    elif spread > 0.3:
        rows.append(("Same exposure?", "CHECK", f"No. The clear film came out up to {spread:.1f} stops brighter in "
                                                "some scans than in others."))
    else:
        rows.append(("Same exposure?", "OK", f"Yes, within {spread:.2f} stops."))

    h = float(np.median([min(c["f"]["headroom"].values()) for c in negs]))
    if h > UNDER_STOPS:
        rows.append(("Bright enough?", "FIX", f"No. The clear film sits about {h:.1f} stops below blown out, so "
                                              f"there's about {h - 0.5:.1f} stops to spare. Brighter gives cleaner "
                                              "color, especially in blue."))
        sugg = suggest_exposure(negs)
        todo.append(f"Expose about {h - 0.5:.1f} stops brighter"
                    + (f", roughly {sugg} with your usual aperture" if sugg else "")
                    + ". The clear film between frames should end up bright but not blown out. The sprocket holes "
                      "will blow out; that's fine.")
    else:
        rows.append(("Bright enough?", "OK", f"Yes. The clear film sits {h:.1f} stops below blown out."))

    bad = flagged("blown out")
    if bad:
        rows.append(("Blown out", "FIX", f"In {_names(bad)}."))
        todo.append("Expose a little darker for the scans with blown-out film.")
    else:
        rows.append(("Blown out", "OK", "Nothing blown out."))

    noisy = [c for c in negs if max(c["f"]["densest"].values()) > NOISE_STOPS]
    if noisy:
        rows.append(("Noise", "CHECK", f"In {len(noisy)} of {len(negs)} scans the densest film (the brightest "
                                       "parts once converted) is down in the sensor noise."))
    else:
        rows.append(("Noise", "OK", "The densest film is above the sensor noise in every scan."))

    mc = [c["f"]["mid/corner"] for c in negs if c["f"]["mid/corner"] is not None]
    bad = flagged("evenness")
    if bad:
        rows.append(("Evenness", "CHECK", f"Changed in {_names(bad)}."))
    elif mc:
        spread = (f" (between {min(mc):+.0f}% and {max(mc):+.0f}%)" if round(min(mc)) != round(max(mc)) else "")
        rows.append(("Evenness", "OK", f"Steady. The clear film is {np.median(mc):+.0f}% brighter mid-edge than at "
                                       f"the corners{spread}, which is normal falloff from the light and lens."))
    else:
        rows.append(("Evenness", "?", "Not enough clear film around the frame to measure."))

    st = [c["f"]["stray"] for c in negs if c["f"]["stray"] is not None]
    bad = flagged("stray light")
    if bad:
        rows.append(("Stray light", "CHECK", f"Went up in {_names(bad)}."))
        todo.append("Cover the sprocket holes and any gaps around the film, so less bare light gets into the lens.")
    elif st:
        rows.append(("Stray light", "OK", f"Steady at about {np.median(st):.1f}% of the light. (The black holder "
                                          "should be pitch black; light there is bouncing around inside the lens.)"))
    else:
        rows.append(("Stray light", "?", "No black holder in view to measure."))

    skipped = [c["name"] for c in caps if not c["f"]["negative"]]
    if skipped:
        rows.append(("Skipped", "", f"{_names(skipped)}: not "
                                    f"{'a color negative' if len(skipped) == 1 else 'color negatives'}."))

    light_bad = any(k == "light" for notes in findings for k, _, _ in notes)
    color_bad = any(k == "color" for notes in findings for k, _, _ in notes)
    conversion = ("the cause is in the conversion: the crop and white balance going into the converter, or its "
                  "settings.")
    if light_bad:
        which = [c["name"] for c, notes in zip(caps, findings) if any(k == "light" for k, _, _ in notes)]
        bottom = f"The light (or stray light) seems to have changed in {_names(which)}. See SCANS WORTH A LOOK."
        todo.append("Shoot the bare light (no film) through a session and run the normal mode on those shots to "
                    "see what it's doing.")
    elif any(status == "FIX" for _, status, _ in rows):
        bottom = "The light looks fine. Fix the exposure first. If a scan still converts badly after that, " + conversion
    else:
        bottom = "The captures look fine. If a scan converts badly, " + conversion
    if color_bad:
        if shift < 5:
            bottom += (f" The film base color shifts (up to {shift:.1f}%) are small: a slight tint at most, not a "
                       "strongly orange frame.")
        if not light_bad:
            todo.append("Let the light warm up a few minutes after switching it on. To see how long yours takes, "
                        "shoot the bare light every minute or two and run the normal mode on those shots.")
    if not todo:
        todo.append("Nothing to fix on the capture side.")
    return rows, bottom, todo


def _names(names, most=8):
    return ", ".join(names) if len(names) <= most else f"{len(names)} scans ({names[0]} to {names[-1]})"


def _f(v, spec, width, unit=""):
    return f"{v:{spec}}{unit}" if v is not None else f"{'-':>{width + len(unit)}s}"


def build_film_report(caps, ri, findings, verdict, stops, use_n, base_box, have_times):
    rows, bottom, todo = verdict
    ref = caps[ri]
    when = (f"shot {caps[0]['time']:%Y-%m-%d %H:%M} to {caps[-1]['time']:%H:%M}" if have_times
            else "in the order given")
    L = ["FILM SCAN CHECK", "=" * 70, f"{len(caps)} scans ({when}), each compared with {ref['name']} (the reference)."]
    if base_box:
        L.append("Clear film measured inside --base " + ",".join(f"{v:g}" for v in base_box) + ".")
    if stops and not use_n:
        L.append("The aperture isn't in the files (manual lens?), so it's assumed the same for every scan.")
    L.append("")

    L.append("THE SHORT ANSWER")
    L += _status_lines(rows)
    L.append("")
    L += _wrapped("Bottom line: " + bottom)
    L.append("")

    if todo:
        L.append("WHAT TO DO")
        for k, item in enumerate(todo, 1):
            L += textwrap.wrap(item, 100, initial_indent=f"  {k}. ", subsequent_indent="     ")
        L.append("")

    L.append("HOW IT WORKS (roughly)")
    L += _wrapped("Around the picture on every negative there's a strip of clear orange film (the \"film base\"). "
                  "It's the same plastic in every frame of a roll, so unlike the picture, it should look exactly "
                  "the same every time. The script finds it in each scan and uses it as a test patch. Everything "
                  "is read straight from the raw sensor data, before any conversion.")
    L += _bullets([
        "Light brightness: the shutter speed and ISO say how much brighter or darker a scan should be than the "
        "reference (1 stop = double or half the light). If the clear film came out brighter or darker than that, "
        "the light itself changed.",
        "Light color: the sprocket holes let the bare light straight through, so their color is the light's own "
        "color. Measured as red compared with blue; \"warmer\" means more red.",
        "Film base color: the clear film's red and blue, each compared with green. It changes if the light's "
        "color changes, or if it's a different film.",
        "Bright enough: how far the clear film sits below the point where the sensor maxes out (\"blown out\"). "
        "Brighter is better, as long as it doesn't blow out.",
        "Noise: the densest film becomes the brightest part of the picture after conversion. If it's very dark "
        "on the sensor, it's mostly noise.",
        "Evenness: brightness of the clear film at the middle of each edge vs. at the corners.",
        "Stray light: brightness of the black holder, which should be pitch black. Any light there is bouncing "
        "around inside the lens, and it adds a haze."])
    L.append("")

    L.append("SCAN BY SCAN")
    head = (f"  {'#':>3s}  {'file':14s} {'min':>6s} {'shutter':>8s} {'ISO':>5s} | {'off by':>7s} {'light':>7s} "
            f"{'film base':>14s} {'below':>9s} | worth a look")
    L.append(head)
    L.append(f"  {'':>3s}  {'':14s} {'':>6s} {'':>8s} {'':>5s} | {'(stops)':>7s} {'color %':>7s} {'red %':>6s} "
             f"{'blue %':>7s} {'blown out':>9s} |")
    L.append("  " + "-" * (len(head) - 2))
    for i, (c, notes) in enumerate(zip(caps, findings)):
        f, r, ex = c["f"], c["row"], c["exif"] or {}
        tm = f"{c['minutes']:6.1f}" if have_times else f"{'#' + str(i):>6s}"
        lead = (f"  {i:3d}{'*' if i == ri else ' '} {c['name'][:14]:14s} {tm} {ex.get('Shutter', '?'):>8s} "
                f"{ex.get('ISO', '?'):>5s} |")
        if not f["negative"]:
            L.append(lead + " not a color negative, skipped")
            continue
        tags = ", ".join(dict.fromkeys(t for _, t, _ in notes))
        L.append(f"{lead} {_f(r['unexplained'], '+7.2f', 7)} {_f(r['light'], '+7.1f', 7)} {r['R/G']:+6.1f} "
                 f"{r['B/G']:+7.1f} {min(f['headroom'].values()):6.1f} st | {tags}".rstrip())
    L.append("")
    tol = f"+/-{EXPOSURE_TOL_AUTO} with auto exposure, +/-{EXPOSURE_TOL} with manual"
    L += _bullets([
        "* is the reference. Every number compares the scan with it.",
        f"off by: how much brighter (+) or darker (-) the clear film came out than the camera settings explain. "
        f"Normal is within {tol}.",
        "light color: the light in the sprocket holes, % warmer (+) or cooler (-).",
        "film base: the clear film, % redder or bluer (+) or less (-), compared with green.",
        "below blown out: how many stops the clear film's brightest channel sits under the sensor's maximum."])
    L.append("")

    L.append("SCANS WORTH A LOOK")
    wrap = textwrap.TextWrapper(width=100, initial_indent="    - ", subsequent_indent="      ")
    shown = False
    for i, (c, notes) in enumerate(zip(caps, findings)):
        if notes:
            shown = True
            L.append(f"  #{i} {c['name']}{' (the reference)' if i == ri else ''}")
            for _, _, text in notes:
                L.extend(wrap.wrap(text))
    if not shown:
        L.append("  None. Every scan matches the reference.")
    L.append("")

    L.append("FILES")
    L.append("  film_over_time.png  the checks above, scan by scan, with the normal range shaded green")
    L.append("  film_check.png      which parts of each scan were measured (green = clear film, red = sprocket")
    L.append("                      holes, blue = black holder, pink = blown out)")
    L.append("  results.csv         all the numbers")
    return "\n".join(L)


def write_film_csv(path, caps, ri, have_times):
    head = ["index", "file", "reference", "negative", "capture_time", "minutes", "shutter", "ISO", "program",
            "set_stops", "measured_stops", "unexplained_stops", "base_R", "base_G", "base_B", "base_R/G",
            "base_B/G", "R/G_change_%", "B/G_change_%", "light_R/B", "light_change_%",
            "headroom_R", "headroom_G", "headroom_B", "densest_R", "densest_G", "densest_B",
            "base_clip_R_%", "base_clip_G_%", "base_clip_B_%", "film_clip_R_%", "film_clip_G_%", "film_clip_B_%",
            "mid/corner_%", "stray_%", "bare_%"]

    def num(v):
        return "" if v is None else f"{v:.4f}"

    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(head)
        for i, c in enumerate(caps):
            f, r, ex = c["f"], c["row"], c["exif"] or {}
            w.writerow([i, c["name"], int(i == ri), int(f["negative"]),
                        c["time"].isoformat(sep=" ") if c["time"] else "",
                        f"{c['minutes']:.2f}" if have_times else "",
                        ex.get("Shutter", ""), ex.get("ISO", ""), ex.get("Program", ""),
                        num(r["set"]), num(r["measured"]), num(r["unexplained"]),
                        *(num(f["base"][ch]) for ch in CHANNELS), num(f["R/G"]), num(f["B/G"]),
                        num(r["R/G"]), num(r["B/G"]), num(f["light R/B"]), num(r["light"]),
                        *(num(f["headroom"][ch]) for ch in CHANNELS), *(num(f["densest"][ch]) for ch in CHANNELS),
                        *(num(f["base_clip"][ch]) for ch in CHANNELS), *(num(f["film_clip"][ch]) for ch in CHANNELS),
                        num(f["mid/corner"]), num(f["stray"]), num(f["bare %"])])


def figure_film_over_time(outdir, caps, ri, have_times, plt):
    x = np.array([c["minutes"] for c in caps]) if have_times else np.arange(len(caps))
    xlabel = "minutes since the first scan" if have_times else "scan number"
    col = {"R": "#d62728", "G": "#2ca02c", "B": "#1f77b4"}
    tol = EXPOSURE_TOL_AUTO if any(is_auto_exposure(c["exif"]) for c in caps) else EXPOSURE_TOL

    def series(get):
        return np.array([get(c) if c["f"]["negative"] and get(c) is not None else np.nan for c in caps], dtype=float)

    def ring_outside(ax, y, lo, hi):
        out = (y < lo) | (y > hi)
        ax.plot(x[out], y[out], "o", ms=10, mfc="none", mec="#d62728", mew=1.5)

    fig, axes = plt.subplots(4, 1, figsize=(10, 15), sharex=True, constrained_layout=True)

    ax = axes[0]
    y = series(lambda c: c["row"]["unexplained"])
    _band(ax, -tol, tol, "normal (red ring = outside it)")
    ax.plot(x, y, "o-", color="#222222", ms=4, lw=1, label="clear film vs. what the settings predict")
    ring_outside(ax, y, -tol, tol)
    _explain(ax, "Did the light's brightness stay steady?",
             "How much brighter (+) or darker (-) the clear film came out than its shutter speed and ISO predict.\n"
             "In the green band = the light didn't change.")
    ax.set_ylabel("stops")

    ax = axes[1]
    _band(ax, -COLOR_TOL, COLOR_TOL, "normal (red ring = outside it)")
    lines = [("light", "s-", "#ff7f0e", 1.8, "light in the sprocket holes: warmer (+) or cooler (-)"),
             ("R/G", "o-", col["R"], 1, "clear film: redder (+) or less red (-)"),
             ("B/G", "o-", col["B"], 1, "clear film: bluer (+) or less blue (-)")]
    for key, style, color, lw, label in lines:
        y = series(lambda c: c["row"][key])
        ax.plot(x, y, style, color=color, ms=4, lw=lw, label=label)
        ring_outside(ax, y, -COLOR_TOL, COLOR_TOL)
    _explain(ax, "Did the light's color stay steady?",
             "% change vs. the reference scan. The sprocket holes show the light itself; the clear film also\n"
             "changes if it's a different film. In the green band = no real change.")
    ax.set_ylabel("% change")

    ax = axes[2]
    bright = series(lambda c: min(c["f"]["headroom"].values()))
    blue = series(lambda c: c["f"]["headroom"]["B"])
    _band(ax, 0.3, 1.0, "aim here")
    ax.axhline(0, color="#d62728", lw=1.2, label="blown out")
    ax.plot(x, bright, "o-", color="#222222", ms=4, lw=1.5, label="clear film, brightest channel")
    ax.plot(x, blue, "o-", color=col["B"], ms=3, lw=1, label="clear film, blue (usually the darkest)")
    low = np.nanmax(blue) if np.isfinite(blue).any() else 4
    ax.set_ylim(max(low + 0.4, 2), -0.3)
    _explain(ax, "Is the exposure bright enough, and the same every scan?",
             "How many stops the clear film sits below blown out (1 stop = half the light). Higher on the chart\n"
             "= brighter. Aim for the green band, in a flat line.")
    ax.set_ylabel("stops below blown out")

    ax = axes[3]
    ax.plot(x, series(lambda c: c["f"]["mid/corner"]), "o-", color="#444444", ms=4, lw=1,
            label="clear film: middle of the edges vs. corners")
    ax.plot(x, series(lambda c: c["f"]["stray"]), "o-", color="#9467bd", ms=4, lw=1,
            label="stray light on the black holder")
    _explain(ax, "Did the evenness or stray light change?",
             "Both in %. Flat lines = no change. A jump means the light got less even, or more light is bouncing\n"
             "around in the lens.")
    ax.set_ylim(bottom=0)
    ax.set_ylabel("%")
    ax.set_xlabel(xlabel)

    for ax in axes:
        ax.axvline(x[ri], color="#999999", lw=1, ls=":", label="reference scan")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8, loc="best", frameon=False)
    for ax in axes[:2]:
        ax.axhline(0, color="black", lw=0.6)
    fig.savefig(os.path.join(outdir, "film_over_time.png"), dpi=130)
    plt.close(fig)


def figure_film_check(outdir, caps, ri, findings, plt):
    others = [i for i in range(len(caps)) if i != ri]
    show = ([ri] + [i for i in others if findings[i]] + [i for i in others if not findings[i]])[:FILM_CHECK_MAX]
    n = len(show)
    ncol = min(n, 4)
    nrow = int(np.ceil(n / ncol))
    fig, axes = plt.subplots(nrow, ncol, figsize=(4.2 * ncol, 3.0 * nrow + 0.8),
                             squeeze=False, constrained_layout=True)
    for k, ax in enumerate(axes.flat):
        ax.set_xticks([])
        ax.set_yticks([])
        if k >= n:
            ax.axis("off")
            continue
        i = show[k]
        c, f = caps[i], caps[i]["f"]
        h, w = c["shape"]
        # Each channel scaled so the clear film is white, like a negative balanced on its base
        rgb = np.dstack([c["preview"][ch] / max(f["base"][ch], 1e-6) for ch in CHANNELS])
        ax.imshow(np.clip(rgb, 0, 1) ** (1 / 2.2), extent=(0, w, h, 0))
        m = f["masks"]
        hb, wb = m["base"].shape
        ov = np.zeros((hb, wb, 4))
        ov[m["holder"]] = (0.1, 0.3, 1.0, 0.55)
        ov[m["base"]] = (0.1, 0.85, 0.2, 0.55)
        ov[m["clipped"]] = (1.0, 0.0, 1.0, 0.8)
        ov[m["bare"]] = (1.0, 0.1, 0.1, 0.85)
        ax.imshow(ov, extent=(0, wb * FILM_BLOCK, hb * FILM_BLOCK, 0), interpolation="nearest")
        ax.set_xlim(0, w)
        ax.set_ylim(h, 0)
        tags = ", ".join(dict.fromkeys(t for _, t, _ in findings[i]))
        ax.set_title(f"#{i} {c['name']}: {'reference' if i == ri else tags or 'OK'}", fontsize=8)
    more = f"\nShowing {n} of {len(caps)} scans: the reference and the ones worth a look first." if len(caps) > n else ""
    legend = ("What was measured in each scan.   Green = clear film (the test patch).   Red = bare light through "
              "the sprocket holes.   Blue = black holder.   Pink = blown out.")
    fig.suptitle(textwrap.fill(legend, 46 * ncol) + more, fontsize=10)     # wrapped so it fits a narrow figure
    fig.savefig(os.path.join(outdir, "film_check.png"), dpi=110)
    plt.close(fig)


def run_film(caps, args, outdir, have_times):
    base_box = parse_box(args.base) if args.base else None
    for i, c in enumerate(caps):
        print(f"Reading {i + 1}/{len(caps)}: {c['name']}")
        planes, _ = load_planes(c["path"])
        c["f"] = measure_film(planes, base_box)
        c["shape"] = planes["R"].shape
        c["preview"] = {ch: planes[ch][::FILM_PREVIEW_STEP, ::FILM_PREVIEW_STEP].astype(np.float32) for ch in CHANNELS}
        del planes

    if args.ref:
        match = [i for i, c in enumerate(caps) if args.ref in (c["name"], os.path.splitext(c["name"])[0])]
        if not match:
            sys.exit(f"--ref {args.ref} isn't one of the scans.")
        ri = match[0]
    else:
        ri = next((i for i, c in enumerate(caps) if c["f"]["negative"]), 0)

    stops, use_n = settings_stops(caps)
    film_rows(caps, ri, stops)
    findings = film_findings(caps, ri)
    verdict = film_verdict(caps, ri, findings, stops)

    report = build_film_report(caps, ri, findings, verdict, stops, use_n, base_box, have_times)
    print()
    print(report)
    with open(os.path.join(outdir, "report.txt"), "w") as fh:
        fh.write(report + "\n")
    write_film_csv(os.path.join(outdir, "results.csv"), caps, ri, have_times)

    print("\nDrawing figures...")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    figure_film_over_time(outdir, caps, ri, have_times, plt)
    figure_film_check(outdir, caps, ri, findings, plt)
    print(f"Done. Results are in {os.path.abspath(outdir)}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def new_run_dir(parent, label=None):
    """Fresh subfolder of parent named after the current time, so earlier runs are kept."""
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + (f"_{label}" if label else "")
    path = os.path.join(parent, stamp)
    n = 2
    while os.path.exists(path):
        path = os.path.join(parent, f"{stamp}_{n}")
        n += 1
    os.makedirs(path)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description="Measure how a scanning light changes over a series of raw captures.")
    ap.add_argument("files", nargs="*",
                    help="two or more raw files of the bare light, or a folder of them; "
                         "leave out to pick a subfolder of captures/ from a list")
    ap.add_argument("--order", choices=("time", "given"), default="time",
                    help="sort by EXIF capture time (default) or keep the order given")
    ap.add_argument("--grid", type=int, default=9, help="number of columns in the analysis grid (default 9)")
    ap.add_argument("--inset", type=float, default=0.05,
                    help="margin trimmed from the lit area on each side, as a fraction (default 0.05)")
    ap.add_argument("--full-frame", action="store_true", help="analyse the whole frame instead of auto-detecting the lit area")
    ap.add_argument("--gain", type=float, default=5, help="amplification for previews.png (default 5)")
    ap.add_argument("--out", default="light_drift_results",
                    help="parent folder for results; each run gets its own timestamped subfolder "
                         "(default light_drift_results)")
    ap.add_argument("--film", action="store_true",
                    help="film scan mode: compare scans of color negatives to tell a bad light from a bad "
                         "exposure or conversion")
    ap.add_argument("--ref", help="film mode: file name of the scan to compare the others against, e.g. the one "
                                  "that converts well (default: the first negative)")
    ap.add_argument("--base", help="film mode: where the clear film base is, as left,top,right,bottom fractions "
                                   "of the frame, e.g. 0.01,0.3,0.04,0.7 (default: found automatically)")
    args = ap.parse_args(argv)

    files = args.files
    if not files:
        files = [choose_subfolder()]
    files = [f for p in files for f in (raw_files_in(p) if os.path.isdir(p) else [p])]
    if len(files) < 2:
        sys.exit(f"Need at least two raw files, found {len(files)}.")
    print(f"Using {len(files)} raw files")

    # Capture times first, so we know the order before reading the big files
    caps = []
    for p in files:
        ex = read_exif(p)
        caps.append({"path": p, "name": os.path.basename(p), "exif": ex,
                     "time": ex["time"] if ex else None})

    have_times = all(c["time"] for c in caps)
    if args.order == "time":
        if have_times:
            caps.sort(key=lambda c: c["time"])
        elif not any(c["exif"] for c in caps):
            print("Note: exifread isn't installed, so files are kept in the order given. "
                  "pip install exifread to sort by capture time.")
        else:
            missing = [c["name"] for c in caps if not c["time"]]
            print(f"Note: no capture time found in {', '.join(missing)}; keeping the order given.")
    if have_times:
        t0 = caps[0]["time"]
        for c in caps:
            c["minutes"] = (c["time"] - t0).total_seconds() / 60
        if len({c["time"] for c in caps}) < len(caps):
            print("Note: some captures share the same timestamp.")
    else:
        for i, c in enumerate(caps):
            c["minutes"] = float(i)

    outdir = new_run_dir(args.out, "film" if args.film else None)
    if args.film:
        run_film(caps, args, outdir, have_times)
        return

    # Read one file at a time to keep memory low; keep only measurements and a small preview
    box = None
    shape = None
    for i, c in enumerate(caps):
        print(f"Reading {i + 1}/{len(caps)}: {c['name']}")
        planes, c["clip"] = load_planes(c["path"])
        if shape is None:
            shape = planes["R"].shape
            if args.full_frame:
                h, w = shape
                dy, dx = int(h * args.inset), int(w * args.inset)
                box = (dy, h - dy, dx, w - dx)
            else:
                box = find_light_region(planes, args.inset)
        elif planes["R"].shape != shape:
            sys.exit(f"{c['name']} has different dimensions from {caps[0]['name']}; "
                     "all captures need to come from the same camera and settings.")
        c["m"] = measure(planes, box, args.grid)
        c["preview"] = {ch: planes[ch][::PREVIEW_STEP, ::PREVIEW_STEP].copy() for ch in CHANNELS}
        del planes

    ref = caps[0]["m"]
    for c in caps:
        c["row"] = summary_row(c["m"], ref)

    report = build_report(caps, have_times)
    print()
    print(report)
    with open(os.path.join(outdir, "report.txt"), "w") as f:
        f.write(report + "\n")
    write_csv(os.path.join(outdir, "results.csv"), caps, have_times)

    print("\nDrawing figures...")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    figure_over_time(outdir, caps, have_times, plt)
    figure_previews(outdir, caps, ref["level"], box, args.gain, have_times, plt)
    figures_first_last(outdir, ref, caps[-1]["m"], plt)
    print(f"Done. Results are in {os.path.abspath(outdir)}")


if __name__ == "__main__":
    main()
