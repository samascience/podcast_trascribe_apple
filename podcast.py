#!/usr/bin/env python3
"""
podscript - paste a podcast link, get the transcript text out.

Usage:
    python3 podscript.py "https://podcasts.apple.com/us/podcast/slug/id1466294689?i=1000669075025"
    python3 podscript.py "https://example.com/feed.xml" --episode "part of the title"
    python3 podscript.py <url> -o out.txt
    python3 podscript.py <url> --no-timestamps
    python3 podscript.py --list "https://podcasts.apple.com/us/podcast/slug/id1466294689"

Strategies, tried in order:
  1. local   - Apple Podcasts' cached TTML (only works if you already opened
               the transcript for that episode in the Podcasts app)
  2. feed    - <podcast:transcript> from the RSS feed (Podcasting 2.0), any of
               vtt / srt / json / text / html
  3. whisper - download the audio and transcribe locally, if a whisper CLI is
               installed (mlx_whisper, whisper, or whisper-cpp)

Stdlib only. No pip installs required for strategies 1 and 2.
"""

import argparse
import glob
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 podscript/1.0"

TTML_CACHE = os.path.expanduser(
    "~/Library/Group Containers/243LU875E5.groups.com.apple.podcasts"
    "/Library/Cache/Assets/TTML"
)
PODCASTS_DB = os.path.expanduser(
    "~/Library/Group Containers/243LU875E5.groups.com.apple.podcasts"
    "/Documents/MTLibrary.sqlite"
)

TT = "{http://www.w3.org/ns/ttml}"
TTM = "{http://www.w3.org/ns/ttml#metadata}"
PC = "{http://podcasts.apple.com/transcript-ttml-internal}"
PODNS = "{https://podcastindex.org/namespace/1.0}"


# ---------------------------------------------------------------- utilities

def log(msg):
    print(f"  {msg}", file=sys.stderr)


def fetch(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


def to_mmss(value):
    """Accept '12.5', '01:02.3', '1:02:03.4' or seconds; return M:SS / H:MM:SS."""
    if value in (None, ""):
        return ""
    try:
        parts = str(value).replace(",", ".").split(":")
        sec = float(parts[-1])
        if len(parts) > 1:
            sec += int(parts[-2]) * 60
        if len(parts) > 2:
            sec += int(parts[-3]) * 3600
    except ValueError:
        return ""
    h, m, s = int(sec) // 3600, (int(sec) % 3600) // 60, int(sec) % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def clean(text):
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


# ------------------------------------------------------------- URL parsing

def parse_source(url):
    """Return dict with collection_id, episode_id, feed_url."""
    out = {"collection_id": None, "episode_id": None, "feed_url": None}
    url = url.strip()

    # bare numeric episode id
    if url.isdigit():
        out["episode_id"] = url
        return out

    parsed = urllib.parse.urlparse(url)

    if "podcasts.apple.com" in parsed.netloc:
        m = re.search(r"/id(\d+)", parsed.path)
        if m:
            out["collection_id"] = m.group(1)
        q = urllib.parse.parse_qs(parsed.query)
        if "i" in q:
            out["episode_id"] = q["i"][0]
    elif "overcast.fm" in parsed.netloc or "pca.st" in parsed.netloc:
        out["page_url"] = url
    else:
        # assume it's an RSS feed or an episode page
        out["feed_url"] = url

    return out


def feed_url_from_collection(collection_id):
    data = json.loads(
        fetch(f"https://itunes.apple.com/lookup?id={collection_id}&entity=podcast")
    )
    for r in data.get("results", []):
        if r.get("feedUrl"):
            return r["feedUrl"]
    return None


def lookup_episodes(collection_id, limit=200):
    """Return Apple's episode list for a show."""
    data = json.loads(
        fetch(
            f"https://itunes.apple.com/lookup?id={collection_id}"
            f"&entity=podcastEpisode&limit={limit}"
        )
    )
    return [r for r in data.get("results", []) if r.get("wrapperType") == "podcastEpisode"]


def resolve_episode(info):
    """Fill in collection_id / feed_url / title from Apple, given an episode id."""
    meta = {"title": None, "feed_url": info.get("feed_url"), "enclosure": None}

    if info.get("collection_id"):
        eps = lookup_episodes(info["collection_id"])
        if eps:
            meta["feed_url"] = meta["feed_url"] or eps[0].get("feedUrl")
        for e in eps:
            if str(e.get("trackId")) == str(info.get("episode_id")):
                meta["title"] = e.get("trackName")
                meta["enclosure"] = e.get("episodeUrl")
                break

    # fall back to the local Podcasts DB for the title
    if not meta["title"] and info.get("episode_id"):
        meta["title"] = db_title(info["episode_id"]) or meta["title"]

    return meta


def db_title(episode_id):
    if not os.path.exists(PODCASTS_DB):
        return None
    try:
        import sqlite3

        with tempfile.TemporaryDirectory() as td:
            # copy so we never touch the live DB (and pick up the WAL)
            for ext in ("", "-wal", "-shm"):
                if os.path.exists(PODCASTS_DB + ext):
                    shutil.copy(PODCASTS_DB + ext, os.path.join(td, "db.sqlite" + ext))
            con = sqlite3.connect(os.path.join(td, "db.sqlite"))
            row = con.execute(
                "SELECT ZTITLE FROM ZMTEPISODE WHERE ZSTORETRACKID=?", (episode_id,)
            ).fetchone()
            con.close()
            return row[0] if row else None
    except Exception:
        return None


# ------------------------------------------------- strategy 1: local cache

def from_local_cache(episode_id):
    if not os.path.isdir(TTML_CACHE):
        return None
    hits = []
    if episode_id:
        hits = glob.glob(f"{TTML_CACHE}/**/*{episode_id}*.ttml", recursive=True)
    if not hits:
        return None
    return parse_ttml(hits[0])


def parse_ttml(path):
    root = ET.parse(path).getroot()
    blocks = []
    for p in root.iter(TT + "p"):
        # Apple nests word-level spans with no whitespace between them,
        # so join on the word spans rather than using itertext().
        words = [w.text or "" for w in p.iter(TT + "span") if w.get(PC + "unit") == "word"]
        text = clean(" ".join(words)) if words else clean("".join(p.itertext()))
        if text:
            blocks.append((to_mmss(p.get("begin")), p.get(TTM + "agent") or "", text))
    return blocks or None


# -------------------------------------------------- strategy 2: RSS feed

def find_feed_item(feed_xml, episode_id=None, title=None, enclosure=None):
    root = ET.fromstring(feed_xml.encode("utf-8", "replace"))
    items = root.iter("item")
    want = clean(title).lower() if title else None

    best = None
    for item in items:
        itext = clean((item.findtext("title") or "")).lower()
        enc = item.find("enclosure")
        eurl = enc.get("url") if enc is not None else ""
        if episode_id and episode_id in ET.tostring(item, encoding="unicode"):
            return item
        if enclosure and eurl and eurl.split("?")[0] == enclosure.split("?")[0]:
            return item
        if want and itext and (want in itext or itext in want):
            best = best or item
    return best


def transcript_from_item(item):
    """Return (url, type) for the best <podcast:transcript> on an item."""
    cands = []
    for t in item.iter(PODNS + "transcript"):
        cands.append((t.get("url"), (t.get("type") or "").lower()))
    for t in item.iter("podcast:transcript"):  # non-namespaced parsers
        cands.append((t.get("url"), (t.get("type") or "").lower()))
    if not cands:
        return None, None
    order = ["json", "vtt", "srt", "text", "html"]

    def rank(c):
        for i, k in enumerate(order):
            if k in (c[1] or ""):
                return i
        return len(order)

    cands.sort(key=rank)
    return cands[0]


def parse_vtt_srt(text):
    blocks = []
    text = text.replace("\r\n", "\n")
    for chunk in re.split(r"\n\s*\n", text):
        lines = [l for l in chunk.strip().split("\n") if l.strip()]
        if not lines:
            continue
        if lines[0].strip().upper().startswith("WEBVTT"):
            lines = lines[1:]
        if lines and re.fullmatch(r"\d+", lines[0].strip()):
            lines = lines[1:]  # SRT counter
        ts = ""
        if lines and "-->" in lines[0]:
            ts = to_mmss(lines[0].split("-->")[0].strip())
            lines = lines[1:]
        speaker = ""
        body = " ".join(lines)
        m = re.match(r"^<v\s+([^>]+)>", body)
        if m:
            speaker = m.group(1)
        body = clean(re.sub(r"<[^>]+>", "", body))
        if body:
            blocks.append((ts, speaker, body))
    return blocks or None


def parse_json_transcript(text):
    data = json.loads(text)
    segs = data.get("segments") if isinstance(data, dict) else data
    if not isinstance(segs, list):
        return None
    blocks = []
    for s in segs:
        if not isinstance(s, dict):
            continue
        body = clean(s.get("body") or s.get("text") or "")
        if body:
            blocks.append((to_mmss(s.get("startTime", s.get("start", ""))),
                           s.get("speaker") or "", body))
    return blocks or None


def parse_plain(text, is_html):
    if is_html:
        text = re.sub(r"(?is)<(script|style).*?</\1>", "", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
    paras = [clean(p) for p in re.split(r"\n\s*\n", html.unescape(text))]
    return [("", "", p) for p in paras if p] or None


def from_feed(feed_url, episode_id=None, title=None, enclosure=None):
    if not feed_url:
        return None
    feed_xml = fetch(feed_url)
    item = find_feed_item(feed_xml, episode_id, title, enclosure)
    if item is None:
        log("episode not found in feed")
        return None
    url, ttype = transcript_from_item(item)
    if not url:
        log("feed has no <podcast:transcript> for this episode")
        return None
    log(f"downloading transcript ({ttype or 'unknown type'})")
    body = fetch(url)
    if "json" in (ttype or "") or url.endswith(".json"):
        return parse_json_transcript(body)
    if "vtt" in (ttype or "") or "srt" in (ttype or "") or url.endswith((".vtt", ".srt")):
        return parse_vtt_srt(body)
    return parse_plain(body, "html" in (ttype or ""))


def enclosure_from_feed(feed_url, episode_id=None, title=None):
    try:
        item = find_feed_item(fetch(feed_url), episode_id, title, None)
        if item is None:
            return None
        enc = item.find("enclosure")
        return enc.get("url") if enc is not None else None
    except Exception:
        return None


# --------------------------------------------------- strategy 3: whisper

def whisper_cli():
    for name in ("mlx_whisper", "whisper", "whisper-cpp"):
        if shutil.which(name):
            return name
    return None


def from_whisper(audio_url, model="base"):
    cli = whisper_cli()
    if not cli:
        log("no whisper CLI found (try: pip install mlx-whisper, or brew install whisper-cpp)")
        return None
    if not audio_url:
        log("no audio URL to transcribe")
        return None

    tmp = tempfile.mkdtemp(prefix="podscript-")
    audio = os.path.join(tmp, "episode.mp3")
    log(f"downloading audio ({cli} will transcribe - this can take a while)")
    with urllib.request.urlopen(
        urllib.request.Request(audio_url, headers={"User-Agent": UA}), timeout=300
    ) as r, open(audio, "wb") as f:
        shutil.copyfileobj(r, f)

    cmd = [cli, audio, "--output_format", "vtt", "--output_dir", tmp]
    if cli != "whisper-cpp":
        cmd += ["--model", model]
    log(f"running: {' '.join(cmd[:2])} ...")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)

    vtts = glob.glob(os.path.join(tmp, "*.vtt"))
    if not vtts:
        return None
    with open(vtts[0]) as f:
        return parse_vtt_srt(f.read())


# ------------------------------------------------------------- rendering

def coalesce(blocks, soft=380, hard=700):
    """Some feeds emit one segment per word. Merge those back into paragraphs.

    Only kicks in when segments really are word/fragment level, so transcripts
    that already come as paragraphs (e.g. Apple's TTML) pass through untouched.
    """
    if not blocks:
        return blocks
    counts = sorted(len(b[2].split()) for b in blocks)
    if counts[len(counts) // 2] > 3:
        return blocks

    merged = []
    ts, spk, buf = blocks[0][0], blocks[0][1], []
    for b_ts, b_spk, text in blocks:
        if b_spk != spk and buf:
            merged.append((ts, spk, " ".join(buf)))
            ts, spk, buf = b_ts, b_spk, []
        if not buf:
            ts = b_ts
        buf.append(text)
        joined = " ".join(buf)
        if (len(joined) >= soft and re.search(r"[.!?]\"?$", text)) or len(joined) >= hard:
            merged.append((ts, spk, joined))
            buf = []
    if buf:
        merged.append((ts, spk, " ".join(buf)))
    return merged


def render(blocks, header="", timestamps=True, speakers=True):
    out = []
    if header:
        out.append(header)
        out.append("=" * min(len(header), 70))
    last = None
    for ts, spk, text in blocks:
        if speakers and spk and spk != last:
            out.append(f"\n[{spk}]")
            last = spk
        out.append(f"[{ts}] {text}" if timestamps and ts else text)
        out.append("")
    return "\n".join(out).strip() + "\n"


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description="Paste a podcast link, get the transcript.")
    ap.add_argument("url", nargs="?", help="Apple Podcasts URL, RSS feed URL, or episode ID")
    ap.add_argument("-o", "--out", help="write to file instead of stdout")
    ap.add_argument("--episode", help="match episode by title substring (for feed URLs)")
    ap.add_argument("--no-timestamps", action="store_true")
    ap.add_argument("--no-speakers", action="store_true")
    ap.add_argument("--list", action="store_true", help="list episodes for a show URL")
    ap.add_argument("--whisper", action="store_true", help="force local whisper transcription")
    ap.add_argument("--model", default="base", help="whisper model (default: base)")
    args = ap.parse_args()

    if not args.url:
        args.url = input("Paste podcast link: ").strip()
    if not args.url:
        sys.exit("no URL given")

    info = parse_source(args.url)

    if args.list:
        if not info.get("collection_id"):
            sys.exit("--list needs an Apple Podcasts show URL")
        for e in lookup_episodes(info["collection_id"]):
            print(f"{e.get('trackId')}  {e.get('trackName')}")
        return

    meta = resolve_episode(info)
    title = args.episode or meta.get("title")
    feed_url = meta.get("feed_url") or info.get("feed_url")
    if not feed_url and info.get("collection_id"):
        feed_url = feed_url_from_collection(info["collection_id"])

    log(f"episode: {title or '(unknown)'}")

    blocks = None
    if args.whisper:
        audio = meta.get("enclosure") or enclosure_from_feed(feed_url, info.get("episode_id"), title)
        blocks = from_whisper(audio, args.model)
    else:
        log("[1/3] checking local Apple Podcasts cache")
        blocks = from_local_cache(info.get("episode_id"))
        if blocks:
            log("found cached transcript")
        else:
            log("[2/3] checking RSS feed for a published transcript")
            try:
                blocks = from_feed(feed_url, info.get("episode_id"), title, meta.get("enclosure"))
            except Exception as e:
                log(f"feed lookup failed: {e}")
            if blocks:
                log("found transcript in feed")
            elif whisper_cli():
                log("[3/3] falling back to local whisper transcription")
                audio = meta.get("enclosure") or enclosure_from_feed(
                    feed_url, info.get("episode_id"), title
                )
                blocks = from_whisper(audio, args.model)

    if not blocks:
        sys.exit(
            "\nNo transcript found.\n"
            "  - Apple transcripts only exist locally after you open them in the\n"
            "    Podcasts app: open the episode > tap the transcript icon > rerun.\n"
            "  - Or install a whisper CLI and rerun with --whisper.\n"
        )

    text = render(
        coalesce(blocks),
        header=title or "",
        timestamps=not args.no_timestamps,
        speakers=not args.no_speakers,
    )

    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
        words = sum(len(b[2].split()) for b in blocks)
        log(f"wrote {len(blocks)} blocks / {words} words -> {args.out}")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
