#!/usr/bin/env python3
"""Rasterise SVG logos to fixed-size PNGs.

Every output is exactly --size pixels. By default the artwork is scaled to fit
inside that box with its aspect ratio intact and centred on a transparent
canvas, which is what channel-logo consumers expect.

    ./svg2png.py ~/Desktop/logos -o ~/Desktop/logos/png
    ./svg2png.py ~/Desktop/logos --size 1884x1293 --margin 4 --bg white
    ./svg2png.py ~/Desktop/logos --fit cover      # fill the box, crop overflow
    ./svg2png.py ~/Desktop/logos --fit stretch    # ignore aspect ratio

Renderer: the first of rsvg-convert, cairosvg, inkscape, ImageMagick (with an
rsvg delegate) or headless Chrome that is present. Force one with --renderer.
Pillow is required for the canvas step: python3 -m pip install pillow
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import concurrent.futures as cf

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow is required: python3 -m pip install pillow")

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "google-chrome", "chromium", "chromium-browser", "microsoft-edge",
]

SIZE_RE = re.compile(r"^\s*(\d+)\s*[x×,]\s*(\d+)\s*$", re.I)


def parse_size(text):
    m = SIZE_RE.match(text)
    if not m:
        raise argparse.ArgumentTypeError("size must look like 1884x1293")
    return int(m.group(1)), int(m.group(2))


# ---------------------------------------------------------------- renderers

def which_chrome():
    for cand in CHROME_CANDIDATES:
        path = cand if os.path.isabs(cand) and os.path.exists(cand) else shutil.which(cand)
        if path:
            return path
    return None


def magick_has_svg():
    exe = shutil.which("magick") or shutil.which("convert")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-list", "delegate"], capture_output=True, text=True,
                             timeout=20).stdout
    except Exception:
        return None
    return exe if re.search(r"^\s*svg\s*=>", out, re.M) else None


def available_renderers():
    found = {}
    if shutil.which("rsvg-convert"):
        found["rsvg"] = shutil.which("rsvg-convert")
    try:
        import cairosvg  # noqa: F401
        found["cairosvg"] = "python module"
    except Exception:
        pass
    if shutil.which("inkscape"):
        found["inkscape"] = shutil.which("inkscape")
    exe = magick_has_svg()
    if exe:
        found["magick"] = exe
    chrome = which_chrome()
    if chrome:
        found["chrome"] = chrome
    return found


def render(renderer, exe, src, dst, w, h, fit):
    """Rasterise src to a PNG of exactly w x h (letterboxed by the caller for
    'contain'; here w/h is the size the artwork itself should occupy)."""
    if renderer == "rsvg":
        cmd = [exe, "-w", str(w), "-h", str(h), "-f", "png", "-o", dst, src]
        if fit != "stretch":
            cmd.insert(1, "--keep-aspect-ratio")
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    elif renderer == "cairosvg":
        import cairosvg
        cairosvg.svg2png(url=src, write_to=dst, output_width=w, output_height=h)
    elif renderer == "inkscape":
        subprocess.run([exe, src, "-w", str(w), "-h", str(h), "-o", dst],
                       check=True, capture_output=True, timeout=120)
    elif renderer == "magick":
        subprocess.run([exe, "-background", "none", "-density", "300", src,
                        "-resize", "%dx%d!" % (w, h), dst],
                       check=True, capture_output=True, timeout=120)
    elif renderer == "chrome":
        render_chrome(exe, src, dst, w, h, fit)
    else:
        raise ValueError("unknown renderer %r" % renderer)


def render_chrome(exe, src, dst, w, h, fit):
    """Let the browser do the scaling: an <img> with object-fit does the work."""
    obj = {"contain": "contain", "cover": "cover", "stretch": "fill"}[fit]
    html = (
        "<!doctype html><meta charset=utf-8>"
        "<style>html,body{margin:0;background:transparent;overflow:hidden}"
        "img{width:%dpx;height:%dpx;object-fit:%s;display:block}</style>"
        "<img src=\"file://%s\">" % (w, h, obj, src.replace('"', "%22"))
    )
    with tempfile.TemporaryDirectory() as tmp:
        page = os.path.join(tmp, "page.html")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(html)
        subprocess.run([
            exe, "--headless", "--disable-gpu", "--hide-scrollbars",
            "--default-background-color=00000000",
            "--force-device-scale-factor=1",
            "--virtual-time-budget=4000",
            "--window-size=%d,%d" % (w, h),
            "--screenshot=%s" % dst,
            "--user-data-dir=%s" % os.path.join(tmp, "profile"),
            "file://" + page,
        ], check=True, capture_output=True, timeout=180)


# ---------------------------------------------------------------- svg sizing

def svg_aspect(path):
    """Best-effort intrinsic aspect ratio (width / height); None if unknown."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    tag = re.search(r"<svg\b[^>]*>", head, re.S | re.I)
    if not tag:
        return None
    tag = tag.group(0)
    vb = re.search(r'viewBox\s*=\s*["\']([^"\']+)["\']', tag, re.I)
    if vb:
        nums = re.findall(r"-?[\d.]+(?:e-?\d+)?", vb.group(1))
        if len(nums) == 4:
            w, h = float(nums[2]), float(nums[3])
            if w > 0 and h > 0:
                return w / h
    dims = {}
    for attr in ("width", "height"):
        m = re.search(r'\b%s\s*=\s*["\']([\d.]+)' % attr, tag, re.I)
        if m:
            dims[attr] = float(m.group(1))
    if dims.get("width") and dims.get("height"):
        return dims["width"] / dims["height"]
    return None


DOCTYPE_RE = re.compile(r"<!DOCTYPE[^>\[]*(\[.*?\])?\s*>", re.S | re.I)
ENTITY_REF_RE = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#)[\w;])[\w.:-]+;")


def sanitise_svg(src, dst):
    """Drop the internal DTD subset that Illustrator exports carry.

    cairosvg (via defusedxml) refuses documents declaring entities, which is
    what an "EntitiesForbidden" failure means. Removing the DOCTYPE and the
    now-dangling &entity; references leaves the artwork untouched.
    """
    with open(src, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    # In Illustrator exports these entities hold namespace URIs, so they are
    # replaced with a stand-in URN rather than deleted: an empty xmlns:x=""
    # would leave any x:… element with an unbound prefix.
    cleaned = ENTITY_REF_RE.sub(lambda m: "urn:x-entity:" + m.group(0)[1:-1],
                                DOCTYPE_RE.sub("", text))
    if cleaned == text:
        return False
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(cleaned)
    return True


def parse_bg(value):
    if not value or value.lower() in ("none", "transparent"):
        return (0, 0, 0, 0)
    from PIL import ImageColor
    rgba = ImageColor.getcolor(value, "RGBA")
    return rgba


# ---------------------------------------------------------------- pipeline

def convert_one(src, outdir, size, fit, margin, bg, renderer, exe, supersample, force):
    name = os.path.splitext(os.path.basename(src))[0] + ".png"
    dst = os.path.join(outdir, name)
    if os.path.exists(dst) and not force:
        return "skip", name, ""

    W, H = size
    inner_w = max(1, int(round(W * (1 - margin / 100.0))))
    inner_h = max(1, int(round(H * (1 - margin / 100.0))))

    aspect = svg_aspect(src)
    if fit == "stretch" or aspect is None:
        rw, rh = inner_w, inner_h
    elif fit == "contain":
        rw, rh = (inner_w, max(1, int(round(inner_w / aspect)))) \
            if inner_w / aspect <= inner_h else (max(1, int(round(inner_h * aspect))), inner_h)
    else:  # cover
        rw, rh = (inner_w, max(1, int(round(inner_w / aspect)))) \
            if inner_w / aspect >= inner_h else (max(1, int(round(inner_h * aspect))), inner_h)

    ss = max(1, supersample)
    with tempfile.TemporaryDirectory() as tmp:
        raw = os.path.join(tmp, "raw.png")

        def attempt(path):
            render(renderer, exe, os.path.abspath(path), raw, rw * ss, rh * ss, fit)

        try:
            attempt(src)
        except Exception as first:
            clean = os.path.join(tmp, "clean.svg")
            try:
                if not sanitise_svg(src, clean):
                    raise first
                attempt(clean)
            except subprocess.CalledProcessError as exc:
                err = (exc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
                return "fail", name, err[-1] if err else "renderer exit %d" % exc.returncode
            except Exception as exc:
                return "fail", name, "%s: %s" % (type(exc).__name__, exc)
        if not os.path.exists(raw) or not os.path.getsize(raw):
            return "fail", name, "renderer produced no output"

        with Image.open(raw) as im:
            art = im.convert("RGBA")
            if art.size != (rw, rh):
                art = art.resize((rw, rh), Image.LANCZOS)
            canvas = Image.new("RGBA", (W, H), bg)
            canvas.alpha_composite(art, ((W - rw) // 2, (H - rh) // 2))
            if bg[3] == 255:
                canvas = canvas.convert("RGB")
            tmp_out = dst + ".part"
            canvas.save(tmp_out, "PNG", optimize=True)
        os.replace(tmp_out, dst)
    return "ok", name, "%dx%d art" % (rw, rh)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("indir", nargs="?", default=".", help="folder of .svg files")
    ap.add_argument("-o", "--outdir", default=None, help="default: INDIR/png")
    ap.add_argument("-s", "--size", type=parse_size, default=(1884, 1293),
                    help="output canvas, WxH (default 1884x1293)")
    ap.add_argument("--fit", choices=("contain", "cover", "stretch"), default="contain")
    ap.add_argument("--margin", type=float, default=0.0,
                    help="empty padding as %% of canvas (default 0)")
    ap.add_argument("--bg", default="transparent",
                    help="canvas colour: transparent (default), white, '#0b0b0b', …")
    ap.add_argument("--renderer", choices=("rsvg", "cairosvg", "inkscape", "magick", "chrome"))
    ap.add_argument("--supersample", type=int, default=1,
                    help="render N× then downsample for smoother edges (default 1)")
    ap.add_argument("-r", "--recursive", action="store_true")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--force", action="store_true", help="overwrite existing PNGs")
    ap.add_argument("--list-renderers", action="store_true")
    args = ap.parse_args()

    found = available_renderers()
    if args.list_renderers:
        for k, v in found.items():
            print("%-10s %s" % (k, v))
        if not found:
            print("none found - install one: brew install librsvg  (or pip install cairosvg)")
        return

    if not found:
        sys.exit("No SVG renderer found. Install one:\n"
                 "  brew install librsvg        # rsvg-convert, best quality\n"
                 "  python3 -m pip install cairosvg\n"
                 "  brew install --cask inkscape\n"
                 "Headless Google Chrome also works if it is installed.")
    renderer = args.renderer or next(k for k in ("rsvg", "cairosvg", "inkscape", "magick", "chrome")
                                     if k in found)
    if renderer not in found:
        sys.exit("Renderer %r not available. Present: %s" % (renderer, ", ".join(found) or "none"))
    exe = found[renderer]

    files = []
    if args.recursive:
        for root, _dirs, names in os.walk(args.indir):
            files += [os.path.join(root, n) for n in names if n.lower().endswith(".svg")]
    else:
        files = [os.path.join(args.indir, n) for n in sorted(os.listdir(args.indir))
                 if n.lower().endswith(".svg")]
    if not files:
        sys.exit("No .svg files in %s" % os.path.abspath(args.indir))

    outdir = args.outdir or os.path.join(args.indir, "png")
    os.makedirs(outdir, exist_ok=True)
    bg = parse_bg(args.bg)

    print("%d SVGs -> %dx%d %s via %s" % (len(files), args.size[0], args.size[1],
                                          args.fit, renderer))
    ok = skip = fail = 0
    with cf.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futs = [pool.submit(convert_one, f, outdir, args.size, args.fit, args.margin,
                            bg, renderer, exe, args.supersample, args.force) for f in files]
        for fut in cf.as_completed(futs):
            status, name, note = fut.result()
            if status == "ok":
                ok += 1
            elif status == "skip":
                skip += 1
            else:
                fail += 1
                print("  FAIL %-45s %s" % (name, note))

    print("%d written, %d already present, %d failed -> %s"
          % (ok, skip, fail, os.path.abspath(outdir)))
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
