#!/usr/bin/env python3
"""Rasterise SVG logos to fixed-size WebP files, mirroring the input tree.

Every output is exactly --size pixels. By default the artwork is scaled to fit
inside that box with its aspect ratio intact and centred on a transparent
canvas, and encoded as lossless WebP with alpha.

    ./svg2webp.py ~/Desktop/logos/svg -o ~/Desktop/logos/webp
    ./svg2webp.py ~/Desktop/logos/svg --size 1884x1293 --margin 4 --bg white
    ./svg2webp.py ~/Desktop/logos/svg --quality 90        # lossy instead
    ./svg2webp.py ~/Desktop/logos/svg --if-exists rename  # keep both versions

Existing .webp files are never overwritten: a name that is already taken is
skipped, or given a -2, -3, … suffix with --if-exists rename.

Subfolders of INDIR are recreated under OUTDIR, so svg/uk/bbc.svg becomes
webp/uk/bbc.webp. Pass --flat to drop everything into one folder instead.

Renderer: the first of rsvg-convert, cairosvg, inkscape, ImageMagick (with an
rsvg delegate) or headless Chrome that is present. Force one with --renderer.

Dependencies: Pillow does the canvas and encode steps. Homebrew and system
pythons refuse `pip install` (PEP 668), so if Pillow is missing the script
creates a virtualenv at scripts/.venv, installs into it and re-execs itself.
That happens once; --no-bootstrap turns it off.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import concurrent.futures as cf

VENV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv")


def venv_python(venv=VENV_DIR):
    sub = "Scripts" if os.name == "nt" else "bin"
    exe = "python.exe" if os.name == "nt" else "python3"
    return os.path.join(venv, sub, exe)


def bootstrap():
    """Homebrew and system pythons refuse `pip install` (PEP 668), so put the
    dependencies in a virtualenv beside this script and re-exec from it."""
    if "--no-bootstrap" in sys.argv or os.environ.get("SVG2WEBP_BOOTSTRAPPED"):
        sys.exit("Pillow is required. Either allow the automatic virtualenv "
                 "(drop --no-bootstrap) or install it yourself:\n"
                 "  python3 -m venv %s && %s -m pip install pillow cairosvg"
                 % (VENV_DIR, venv_python()))
    py = venv_python()
    if not os.path.exists(py):
        print("Pillow not found; creating a virtualenv at %s" % VENV_DIR, file=sys.stderr)
        try:
            subprocess.run([sys.executable, "-m", "venv", VENV_DIR], check=True)
        except subprocess.CalledProcessError:
            sys.exit("Could not create a virtualenv. Install Pillow another way, "
                     "then re-run with --no-bootstrap.")
    wanted = ["pillow"]
    if not (shutil.which("rsvg-convert") or shutil.which("inkscape")):
        wanted.append("cairosvg")  # no system renderer present, so bring one
    print("Installing %s into the virtualenv (one time)" % ", ".join(wanted), file=sys.stderr)
    try:
        subprocess.run([py, "-m", "pip", "install", "-q", "--upgrade"] + wanted, check=True)
    except subprocess.CalledProcessError:
        sys.exit("Dependency install failed. Run it by hand:\n  %s -m pip install %s"
                 % (py, " ".join(wanted)))
    os.environ["SVG2WEBP_BOOTSTRAPPED"] = "1"
    for var in ("PYTHONPATH", "PYTHONHOME"):
        os.environ.pop(var, None)  # do not let the outer env shadow the venv
    os.execv(py, [py, os.path.abspath(__file__)] + sys.argv[1:])


try:
    from PIL import Image, features
except ImportError:
    bootstrap()

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "google-chrome", "chromium", "chromium-browser", "microsoft-edge",
]

SIZE_RE = re.compile(r"^\s*(\d+)\s*[x×,]\s*(\d+)\s*$", re.I)
WEBP_MAX = 16383  # hard limit of the WebP container


def parse_size(text):
    m = SIZE_RE.match(text)
    if not m:
        raise argparse.ArgumentTypeError("size must look like 1884x1293")
    w, h = int(m.group(1)), int(m.group(2))
    if not (0 < w <= WEBP_MAX and 0 < h <= WEBP_MAX):
        raise argparse.ArgumentTypeError("WebP dimensions must be 1..%d" % WEBP_MAX)
    return w, h


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
    """Rasterise src to a PNG of exactly w x h (the size the artwork itself
    should occupy; the caller pads it onto the final canvas)."""
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
    what an "EntitiesForbidden" failure means. In these exports the entities
    hold namespace URIs, so they are replaced with a stand-in URN rather than
    deleted: an empty xmlns:x="" would leave any x:… element unbound.
    """
    with open(src, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
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
    return ImageColor.getcolor(value, "RGBA")


def free_path(path, mode):
    """Return a path that does not exist yet, or None to skip.

    Nothing already on disk is ever replaced.
    """
    if not os.path.exists(path):
        return path
    if mode == "skip":
        return None
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists("%s-%d%s" % (stem, n, ext)):
        n += 1
    return "%s-%d%s" % (stem, n, ext)


# ---------------------------------------------------------------- pipeline

def convert_one(src, dst_base, size, fit, margin, bg, renderer, exe,
                supersample, quality, if_exists):
    label = os.path.basename(dst_base) + ".webp"
    dst = free_path(dst_base + ".webp", if_exists)
    if dst is None:
        return "skip", label, ""
    label = os.path.basename(dst)

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
    os.makedirs(os.path.dirname(dst), exist_ok=True)
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
                return "fail", label, err[-1] if err else "renderer exit %d" % exc.returncode
            except Exception as exc:
                return "fail", label, "%s: %s" % (type(exc).__name__, exc)
        if not os.path.exists(raw) or not os.path.getsize(raw):
            return "fail", label, "renderer produced no output"

        with Image.open(raw) as im:
            art = im.convert("RGBA")
            if art.size != (rw, rh):
                art = art.resize((rw, rh), Image.LANCZOS)
            canvas = Image.new("RGBA", (W, H), bg)
            canvas.alpha_composite(art, ((W - rw) // 2, (H - rh) // 2))
            if bg[3] == 255:
                canvas = canvas.convert("RGB")
            opts = {"method": 6}
            if quality is None:
                opts["lossless"] = True
            else:
                opts["lossless"] = False
                opts["quality"] = quality
            tmp_out = os.path.join(tmp, "out.webp")
            canvas.save(tmp_out, "WEBP", **opts)
        # Re-check at the last moment: a concurrent run may have taken the name.
        final = free_path(dst, if_exists)
        if final is None:
            return "skip", label, ""
        shutil.move(tmp_out, final)
        label = os.path.basename(final)
    return "ok", label, "%dx%d art, %.0f kB" % (rw, rh, os.path.getsize(final) / 1024.0)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("indir", nargs="?", default=".", help="folder of .svg files")
    ap.add_argument("-o", "--outdir", default=None, help="default: INDIR/../webp")
    ap.add_argument("-s", "--size", type=parse_size, default=(1884, 1293),
                    help="output canvas, WxH (default 1884x1293)")
    ap.add_argument("--fit", choices=("contain", "cover", "stretch"), default="contain")
    ap.add_argument("--margin", type=float, default=0.0,
                    help="empty padding as %% of canvas (default 0)")
    ap.add_argument("--bg", default="transparent",
                    help="canvas colour: transparent (default), white, '#0b0b0b', …")
    ap.add_argument("-q", "--quality", type=int, default=None,
                    help="lossy WebP at this quality (0-100); default is lossless")
    ap.add_argument("--if-exists", choices=("skip", "rename"), default="skip",
                    help="existing .webp files are never overwritten: skip them "
                         "(default) or write alongside with a -2 suffix")
    ap.add_argument("--flat", action="store_true",
                    help="do not mirror subfolders; write every file into OUTDIR")
    ap.add_argument("--renderer", choices=("rsvg", "cairosvg", "inkscape", "magick", "chrome"))
    ap.add_argument("--supersample", type=int, default=1,
                    help="render N× then downsample for smoother edges (default 1)")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--list-renderers", action="store_true")
    ap.add_argument("--no-bootstrap", action="store_true",
                    help="never create the .venv; fail if dependencies are missing")
    args = ap.parse_args()

    if args.quality is not None and not 0 <= args.quality <= 100:
        sys.exit("--quality must be 0-100")
    if not features.check("webp"):
        sys.exit("This Pillow build has no WebP support: python3 -m pip install -U pillow")

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
                 "  %s -m pip install cairosvg\n"
                 "  brew install --cask inkscape\n"
                 "Headless Google Chrome also works if it is installed."
                 % (sys.executable if os.path.exists(venv_python()) else "python3"))
    renderer = args.renderer or next(k for k in ("rsvg", "cairosvg", "inkscape", "magick", "chrome")
                                     if k in found)
    if renderer not in found:
        sys.exit("Renderer %r not available. Present: %s" % (renderer, ", ".join(found) or "none"))
    exe = found[renderer]

    indir = os.path.abspath(args.indir)
    if not os.path.isdir(indir):
        sys.exit("Not a folder: %s" % indir)
    outdir = os.path.abspath(args.outdir) if args.outdir \
        else os.path.join(os.path.dirname(indir.rstrip(os.sep)), "webp")

    jobs = []
    for root, dirs, names in os.walk(indir):
        dirs[:] = [d for d in sorted(dirs) if not d.startswith(".")]
        if os.path.abspath(root) == outdir:
            dirs[:] = []
            continue
        for n in sorted(names):
            if not n.lower().endswith(".svg"):
                continue
            src = os.path.join(root, n)
            rel = os.path.relpath(root, indir)
            sub = "" if args.flat or rel == "." else rel
            jobs.append((src, os.path.join(outdir, sub, os.path.splitext(n)[0])))
    if not jobs:
        sys.exit("No .svg files under %s" % indir)

    os.makedirs(outdir, exist_ok=True)
    bg = parse_bg(args.bg)
    encoding = "lossless" if args.quality is None else "quality %d" % args.quality

    print("%d SVGs -> %dx%d %s, %s WebP via %s\n%s -> %s"
          % (len(jobs), args.size[0], args.size[1], args.fit, encoding, renderer, indir, outdir))

    ok = skipped = failed = 0
    with cf.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futs = [pool.submit(convert_one, src, base, args.size, args.fit, args.margin, bg,
                            renderer, exe, args.supersample, args.quality, args.if_exists)
                for src, base in jobs]
        for fut in cf.as_completed(futs):
            status, name, note = fut.result()
            if status == "ok":
                ok += 1
            elif status == "skip":
                skipped += 1
            else:
                failed += 1
                print("  FAIL %-45s %s" % (name, note))

    print("%d written, %d left alone (already existed), %d failed -> %s"
          % (ok, skipped, failed, outdir))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
