"""
build_twin_data.py -- rebuild data.json for the surface digital twin from the raw measurement files in raw/.

    python build_twin_data.py

Input  raw/<Plate>/<Sample>/<Sample>_<Plate>_<Field>/
           <id>_HL1..HL10.csv, <id>_VL1..VL10.csv   height profiles exported by the 3D microscope
           <id>_2D.png                                 colour height map (lossless copy of the instrument export)
       img/<Sample>_<Plate>_<Field>_{M,2D}.jpg         optical image and height map for display
Output data.json (read by index.html)

For every field: profiles are read, end padding is masked, roughness is computed (least-squares line removal,
Gaussian filter lambda_c = 0.8 mm per ISO 16610-21 as a normalized convolution, lambda_c/2 removed at each end,
ISO 21920-2 parameters), and a 256 x 192 relief is rebuilt from the colour height map by fitting z = a*h + b
against the profiles of the same field. Reliefs with R^2 < 0.5 are rejected.
"""
import base64
import csv
import datetime as dt
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"

LAMBDA_C_UM = 800.0
Z_STEP = 0.02                      # um per int16 count in data.json
MIN_SURF_R2 = 0.5
EXPECTED_CAL = {50: 2.0793, 30: 3.4858}

# Randomized layout of plate P001 (rows from the recoater start side) and power levels.
PLATE_POSITIONS = {
    "P001": {
        "S07": "R1C1", "S12": "R1C2", "S10": "R1C3", "S08": "R1C4", "S02": "R1C5",
        "S04": "R2C1", "S03": "R2C2", "S15": "R2C3", "S11": "R2C4", "S13": "R2C5",
        "S01": "R3C1", "S14": "R3C2", "S05": "R3C3", "S06": "R3C4", "S09": "R3C5",
    },
}


def power_level(sample):
    """S01-S05: Min (210 W), S06-S10: Avg (280 W), S11-S15: Max (350 W)."""
    return ["Min", "Avg", "Max"][((int(sample[1:]) - 1) % 15) // 5]


# ------------------------------------------------------------------ profiles and roughness
def read_profile(path):
    """Parse one microscope height-profile CSV (Latin-1, 9-line header)."""
    with open(path, encoding="latin-1", newline="") as fh:
        rows = list(csv.reader(fh))
    head = {r[0]: r[1:] for r in rows[:9] if r}
    z = np.array([float(r[0]) for r in rows[9:] if r and r[0].strip()], dtype=float)
    return {
        "z": z,
        "count": int(float(head["Data cnt"][0])),
        "length": float(head["Length"][0]),
        "start": [float(v) for v in head["Starting position 1"][:2]],
        "end": [float(v) for v in head["End Point 1"][:2]],
        "date": dt.datetime.strptime(head["Date saved"][0], "%m/%d/%Y").date(),
    }


def padding_bounds(z):
    """Indices [lo, hi) after dropping repeated identical values at both ends (instrument padding)."""
    lo, hi = 0, len(z)
    while hi - lo > 2 and z[hi - 1] == z[hi - 2]:
        hi -= 1
    while hi - lo > 2 and z[lo] == z[lo + 1]:
        lo += 1
    return lo, hi


def roughness(z, dx, lam=LAMBDA_C_UM):
    """Levelled, Gaussian-filtered roughness parameters of one profile (dx and lam in um)."""
    x = np.arange(len(z)) * dx
    z = z - np.polyval(np.polyfit(x, z, 1), x)
    alpha = math.sqrt(math.log(2) / math.pi)
    half = int(round(lam / dx))
    k = np.arange(-half, half + 1) * dx
    w = np.exp(-math.pi * (k / (alpha * lam)) ** 2)
    mean = np.convolve(z, w, "full")[half:half + len(z)] / np.convolve(np.ones_like(z), w, "full")[half:half + len(z)]
    r = z - mean
    trim = int(round(lam / 2 / dx))
    r = r[trim:len(r) - trim]
    ra, rq = float(np.mean(np.abs(r))), float(np.sqrt(np.mean(r ** 2)))
    per = int(round(lam / dx))
    n_sl = len(r) // per
    rz = float(np.mean([np.ptp(r[i * per:(i + 1) * per]) for i in range(n_sl)])) if n_sl else float("nan")
    return {"Ra": ra, "Rq": rq, "Rz": rz, "Rt": float(np.ptp(r)),
            "Rsk": float(np.mean(r ** 3) / rq ** 3), "Rku": float(np.mean(r ** 4) / rq ** 4)}


def encode(z):
    q = np.round((z - z.mean()) / Z_STEP)
    if np.abs(q).max() > 32767:
        raise ValueError("profile range exceeds int16 encoding")
    return z.mean(), base64.b64encode(q.astype("<i2").tobytes()).decode("ascii")


# ------------------------------------------------------------------ relief from the colour height map
def reconstruct_surface(src, lines, grid_w=256, grid_h=192):
    """Areal relief from the colour height map: jet hue -> 0-1 height, z = a*h + b fitted to the profiles."""
    im = np.asarray(Image.open(src).convert("RGB")).astype(float) / 255
    colourful = (im.max(2) - im.min(2)) > 60 / 255
    cc, rc = colourful.sum(0), colourful.sum(1)
    cols = np.where(cc > 0.8 * cc.max())[0]
    rows = np.where(rc > 0.8 * rc.max())[0]
    crop = im[rows.min():rows.max() + 1, cols.min():cols.max() + 1]
    r, g, b = crop[..., 0], crop[..., 1], crop[..., 2]
    mx, mn = crop.max(2), crop.min(2)
    d = np.maximum(mx - mn, 1e-6)
    hue = np.where(mx == r, ((g - b) / d) % 6, np.where(mx == g, (b - r) / d + 2, (r - g) / d + 4)) * 60
    hval = (240 - np.clip(hue, 0, 240)) / 240
    invalid = (mx - mn) < 0.25                     # axis frame, markers: no hue -> nearest valid pixel
    if invalid.any():
        idx = ndimage.distance_transform_edt(invalid, return_distances=False, return_indices=True)
        hval = hval[tuple(idx)]
    H, W = hval.shape
    xs, ys = [], []
    for ln in lines:
        z = ln["_z"][ln["lo"]:ln["hi"]]
        t = np.arange(ln["lo"], ln["hi"]) / (ln["cnt"] - 1)
        px = ln["x0"] + (ln["x1"] - ln["x0"]) * t
        py = ln["y0"] + (ln["y1"] - ln["y0"]) * t
        xs.append(hval[np.clip((py / 2160 * H).astype(int), 0, H - 1), np.clip((px / 2880 * W).astype(int), 0, W - 1)])
        ys.append(z)
    X, Y = np.concatenate(xs), np.concatenate(ys)
    a, b0 = np.polyfit(X, Y, 1)
    res = Y - (a * X + b0)
    r2 = 1 - res.var() / Y.var()
    if r2 < MIN_SURF_R2:
        return {"rejected": True, "r2": round(float(r2), 3), "rms": round(float(res.std()), 2)}
    grid = np.asarray(Image.fromarray(hval.astype(np.float32), mode="F").resize((grid_w, grid_h), Image.BOX))
    z = a * grid + b0
    zref = float(z.mean())
    q = np.round((z - zref) / 0.1).astype("<i2")
    return {
        "w": grid_w, "h": grid_h, "step": 0.1, "zref": round(zref, 3),
        "z": base64.b64encode(q.tobytes()).decode("ascii"),
        "r2": round(float(r2), 3), "rms": round(float(res.std()), 2),
        "crop": [round(cols.min() / im.shape[1], 5), round(rows.min() / im.shape[0], 5),
                 round((cols.max() + 1) / im.shape[1], 5), round((rows.max() + 1) / im.shape[0], 5)],
    }


# ------------------------------------------------------------------ build
FIELD_RE = re.compile(r"^(S\d{2})_(P\d{3})_(C[1-5]|Main)$")
FIELD_ORDER = ["C1", "C2", "C3", "C4", "C5", "Main"]


def build_field(folder, sample, plate, field):
    csvs = sorted(folder.glob(f"{folder.name}_x*_[HV]L*.csv"))
    if not csvs:
        return None
    mag = int(re.search(r"_x(\d{2})_", csvs[0].name).group(1))
    mid = f"{folder.name}_x{mag}"
    lines, dates, issues = [], [], []
    for d in "HV":
        for i in range(1, 11):
            f = folder / f"{mid}_{d}L{i}.csv"
            if not f.exists():
                issues.append(f"missing {f.name}")
                continue
            p = read_profile(f)
            lo, hi = padding_bounds(p["z"])
            dx = p["length"] / (p["count"] - 1)
            ref = roughness(p["z"][lo:hi], dx)
            mean, b64 = encode(p["z"])
            dates.append(p["date"])
            lines.append({
                "_z": p["z"], "d": d, "n": i, "x0": p["start"][0], "y0": p["start"][1],
                "x1": p["end"][0], "y1": p["end"][1], "cnt": p["count"], "len": p["length"],
                "zmean": round(mean, 4), "z": b64, "lo": lo, "hi": hi,
                "ref": {k: round(ref[k], 4) for k in ("Ra", "Rq", "Rz", "Rt", "Rsk", "Rku")},
            })
    surf, src = None, folder / f"{mid}_2D.png"
    if src.exists() and lines:
        surf = reconstruct_surface(src, lines)
        if surf.get("rejected"):
            issues.append(f"height map does not match the profiles (relief R2={surf['r2']:.2f}); relief not shown")
            surf = None
    for ln in lines:
        del ln["_z"]
    imgs = {k: f"img/{folder.name}_{k}.jpg" for k in ("M", "2D") if (HERE / "img" / f"{folder.name}_{k}.jpg").exists()}
    return {
        "id": mid, "sample": sample, "plate": plate, "spot": field, "mag": mag,
        "cal": round(lines[0]["len"] / (lines[0]["cnt"] - 1), 5) if lines else EXPECTED_CAL[mag],
        "date": min(dates).isoformat() if dates else None,
        "complete": len(lines) == 20 and surf is not None, "missing": "",
        "issues": issues, "img": imgs, "lines": lines, "surf": surf,
    }


def main():
    spots = []
    for folder in sorted(RAW.glob("P*/S*/*")):
        m = FIELD_RE.match(folder.name)
        if not (folder.is_dir() and m):
            continue
        sample, plate, field = m.groups()
        spot = build_field(folder, sample, plate, field)
        if spot:
            spots.append(spot)
            s = spot["surf"]
            print(f"{spot['id']:<20} {len(spot['lines']):>2} profiles  "
                  + (f"relief R2 = {s['r2']:.3f}, RMS = {s['rms']:.1f} um" if s else "no relief"))
    spots.sort(key=lambda s: (s["plate"], s["sample"], FIELD_ORDER.index(s["spot"])))
    plates = {p: [{"sample": sm, "pos": rc, "level": power_level(sm)} for sm, rc in pos.items()]
              for p, pos in PLATE_POSITIONS.items()}
    data = {"generated": dt.date.today().isoformat(), "zStep": Z_STEP, "lambdaC_um": LAMBDA_C_UM,
            "plates": plates, "spots": spots}
    (HERE / "data.json").write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    print(f"data.json: {len(spots)} fields, {(HERE / 'data.json').stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    sys.exit(main())
