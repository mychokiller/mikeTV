#!/usr/bin/env python3
"""Download every tvg-logo image referenced by an M3U playlist.

Usage:
    ./fetch_logos.py PLAYLIST.m3u [-o OUTDIR] [-j N] [--name-mode channel|source]
                     [--force] [--dry-run]

Files are named after the channel (tvg-name) by default, so a playlist where
50 channels share one placeholder logo produces 50 correctly-named files while
the image itself is fetched once. Use --name-mode source to keep the original
remote filenames and skip that duplication.
"""

import argparse
import concurrent.futures as cf
import mimetypes
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"

ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
EXTS = {".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".bmp", ".avif"}
CT_EXT = {
    "image/svg+xml": ".svg",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/avif": ".avif",
}


def slugify(text, fallback="logo"):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s.-]", "", text).strip()
    text = re.sub(r"[\s_]+", "_", text)
    text = text.strip("._-")
    return text or fallback


def ext_from_url(url):
    """Pick an extension from the URL path.

    Handles wiki-style CDN paths such as
    .../Sky_Sports_F1_2025.svg/revision/latest?cb=123 where the real filename
    is not the last path segment.
    """
    path = urllib.parse.urlsplit(url).path
    for seg in reversed([s for s in path.split("/") if s]):
        ext = os.path.splitext(urllib.parse.unquote(seg))[1].lower()
        if ext in EXTS:
            return ext
    return ""


def ext_from_headers(headers, body):
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype in CT_EXT:
        return CT_EXT[ctype]
    guess = mimetypes.guess_extension(ctype) if ctype else None
    if guess:
        return ".jpg" if guess == ".jpe" else guess
    head = body[:512].lstrip()
    if head.startswith(b"<?xml") or b"<svg" in head.lower():
        return ".svg"
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if body[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return ".webp"
    if body[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ".img"


def parse_playlist(path):
    """Yield (channel_name, logo_url) in playlist order, deduplicated."""
    seen = set()
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.startswith("#EXTINF"):
                continue
            attrs = dict(ATTR_RE.findall(line))
            url = attrs.get("tvg-logo") or attrs.get("tv-logo") or ""
            url = url.strip()
            if not url.lower().startswith(("http://", "https://")):
                continue
            name = attrs.get("tvg-name") or line.rsplit(",", 1)[-1].strip()
            key = (name, url)
            if key in seen:
                continue
            seen.add(key)
            yield name, url


def target_name(name, url, mode):
    ext = ext_from_url(url)
    if mode == "source":
        path = urllib.parse.urlsplit(url).path
        base = ""
        for seg in reversed([s for s in path.split("/") if s]):
            seg = urllib.parse.unquote(seg)
            if os.path.splitext(seg)[1].lower() in EXTS:
                base = os.path.splitext(seg)[0]
                break
        if not base:
            base = os.path.splitext(os.path.basename(path))[0]
        return slugify(base), ext
    return slugify(name), ext


def encode_url(url):
    """Percent-encode spaces and other stray characters in the path/query."""
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/%:@&=+$,~")
    query = urllib.parse.quote(urllib.parse.unquote(parts.query), safe="/?:@&=+$,~%")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def fetch(url, timeout=30):
    url = encode_url(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "image/avif,image/webp,image/svg+xml,image/*,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers


def main():
    ap = argparse.ArgumentParser(description="Download tvg-logo images from an M3U playlist.")
    ap.add_argument("playlist")
    ap.add_argument("-o", "--outdir", default="logos")
    ap.add_argument("-j", "--jobs", type=int, default=8)
    ap.add_argument("--name-mode", choices=("channel", "source"), default="channel",
                    help="channel: one file per channel (default). source: keep remote filenames.")
    ap.add_argument("--force", action="store_true", help="re-download files that already exist")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    entries = list(parse_playlist(args.playlist))
    if not entries:
        sys.exit("No tvg-logo URLs found in %s" % args.playlist)

    os.makedirs(args.outdir, exist_ok=True)

    # Plan: resolve output names, keep them unique, group work by URL so a
    # shared placeholder is fetched once and copied.
    used, plan = set(), {}
    for name, url in entries:
        base, ext = target_name(name, url, args.name_mode)
        stem, n = base, 2
        while (stem + (ext or "")) .lower() in used:
            stem = "%s_%d" % (base, n)
            n += 1
        used.add((stem + (ext or "")).lower())
        plan.setdefault(url, []).append(stem + ext if ext else stem)

    if args.dry_run:
        for url, names in plan.items():
            print("%s -> %s" % (url, ", ".join(names)))
        print("\n%d channels, %d unique URLs" % (len(entries), len(plan)))
        return

    done = skipped = failed = 0
    lock_out = sys.stdout

    def work(item):
        url, names = item
        wanted = [os.path.join(args.outdir, n) for n in names]
        if not args.force and all(os.path.exists(p) and os.path.getsize(p) for p in wanted):
            return "skip", url, names[0], ""
        try:
            body, headers = fetch(url, args.timeout)
        except Exception as exc:  # network, TLS, malformed URL, decode errors
            return "fail", url, names[0], "%s: %s" % (type(exc).__name__, exc)
        if not body:
            return "fail", url, names[0], "empty response"
        ext = ext_from_headers(headers, body)
        for path in wanted:
            if not os.path.splitext(path)[1]:
                path += ext
            elif os.path.splitext(path)[1].lower() != ext and ext != ".img":
                path = os.path.splitext(path)[0] + ext
            tmp = path + ".part"
            with open(tmp, "wb") as fh:
                fh.write(body)
            os.replace(tmp, path)
        return "ok", url, os.path.basename(wanted[0]), "%d B" % len(body)

    with cf.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for status, url, name, note in pool.map(work, plan.items()):
            if status == "ok":
                done += 1
                print("  ok   %-45s %s" % (name, note), file=lock_out)
            elif status == "skip":
                skipped += 1
            else:
                failed += 1
                print("  FAIL %-45s %s\n       %s" % (name, note, url), file=lock_out)

    print("\n%d downloaded, %d already present, %d failed (%d channels, %d unique URLs) -> %s"
          % (done, skipped, failed, len(entries), len(plan), os.path.abspath(args.outdir)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
