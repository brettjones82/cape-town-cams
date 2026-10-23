#!/usr/bin/env python3
"""Daily stream check for index.html.

A YouTube live stream gets a new video ID whenever its owner restarts it, and the old ID dies.
For each cam below, this reads the channel's public RSS feed, takes the newest video whose title
matches the cam, and if that differs from the ID in index.html it swaps the new one in (after
checking YouTube allows it to be embedded). No API key needed.

Run locally:  python3 scripts/heal_streams.py   (set CAMS_PAGE to point at another copy of the page)
"""
import html
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PAGE = Path(os.environ.get("CAMS_PAGE", Path(__file__).resolve().parent.parent / "index.html"))

VANILLA = "UC9Ljd016Si9rv3KS-5CbpGg"          # @VanillaISP
REEF = "UCef_Yfk6baScsTCcDxzXE5Q"             # @Reef-SA
WAVE_WATCHERS = "UCf6fbt2V_PXTFtdg1fruTDA"    # @MuizenbergWaveWatchers
TABLE_MTN_LS = "UCLaxio5CTbT1gl5i8FB2-yw"     # @TableMountainLiveStream
TCS = "UCtLaqdQYL-JPh2YarCGOPOw"              # @TCSLiveOnline
CAPE_OCEAN = "UCLwTi-vM5zaadH1ytPo-rTg"       # @CapeOceanAdventures

# Cam name exactly as in index.html -> (channel, phrases the title must contain, phrases it must not).
# Keep phrases specific: the channels also post clips (e.g. "Newlands fire over Table Mountain").
RULES = {
    "Table Mountain": (VANILLA, ["live stream table mountain"], []),
    "Signal Hill": (VANILLA, ["live stream signal hill"], ["cbd"]),
    "Signal Hill and CBD": (VANILLA, ["signal hill and cape town cbd"], []),
    "Sea Point and Lion's Head": (VANILLA, ["sea point and lions head"], []),
    "Table Mountain from Bloubergstrand": (TABLE_MTN_LS, ["bloubergstrand beach camera"], []),
    "Muizenberg Beach": (REEF, ["muizenberg beach live"], []),
    # Muizenberg Surf isn't here: the page embeds Wave Watchers' channel live stream directly.
    "Strand Beach": (CAPE_OCEAN, ["strand beach cam"], []),
    "Langebaan Lagoon": (TABLE_MTN_LS, ["langebaan lagoon camera"], []),
    "Nature's Valley, Plettenberg Bay": (TCS, ["natures valley"], []),
}


def get(url):
    """Returns (status, body, headers). Retries network errors and 5xx; 4xx is an answer, not a failure."""
    err = None
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (cape-town-cams stream check)"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read().decode(errors="replace"), r.headers
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return e.code, "", e.headers
            err = e
        except Exception as e:  # timeouts, DNS, resets
            err = e
        time.sleep(5)
    raise RuntimeError(str(err))


def fetch(url):
    return get(url)[:2]


SITE = "brettjones82.github.io"


def embed_blocked(url):
    """Why a non-YouTube player won't load inside our page, or None if it should. These can't be fixed
    automatically (the owner chose to lock them), so they're reported as problems."""
    status, body, headers = get(url)
    if status != 200:
        return f"player answers HTTP {status}"
    if (headers.get("X-Frame-Options") or "").strip():
        return f"player only loads on its own site (X-Frame-Options: {headers['X-Frame-Options']})"
    csp = headers.get("Content-Security-Policy") or ""
    ancestors = re.search(r"frame-ancestors([^;]*)", csp)
    if ancestors and "*" not in ancestors.group(1) and SITE not in ancestors.group(1):
        return f"player only loads on {ancestors.group(1).strip()}"
    if re.search(r"domainlockenabled\s*=\s*1", body):
        return "IPCamLive domain lock is on"
    return None


def oembed(video):
    return f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video}&format=json"


def feed(channel):
    """Newest first as (published, video id, normalised title). The feed itself isn't strictly date-ordered."""
    status, xml = fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel}")
    if status != 200:
        raise RuntimeError(f"HTTP {status}")
    entries = re.findall(r"<yt:videoId>([^<]+)</yt:videoId>.*?<title>([^<]+)</title>.*?<published>([^<]+)</published>", xml, re.S)
    norm = lambda t: " ".join(html.unescape(t).lower().split())
    return sorted(((pub, vid, norm(title)) for vid, title, pub in entries), reverse=True)


def main():
    page = PAGE.read_text()
    feeds, changes, problems = {}, [], []
    for cam, (channel, must, must_not) in RULES.items():
        m = re.search(r'\{ id: "([^"]+)", name: "' + re.escape(cam) + '"', page)
        if not m:
            problems.append(f"{cam}: not found in index.html (renamed?)")
            continue
        current = m.group(1)
        try:
            if channel not in feeds:
                feeds[channel] = feed(channel)
        except RuntimeError as e:
            problems.append(f"{cam}: couldn't read the channel feed ({e})")
            continue
        hits = [vid for _, vid, title in feeds[channel]
                if all(p in title for p in must) and not any(p in title for p in must_not)]
        if not hits or hits[0] == current:
            # Nothing newer to switch to. YouTube's oEmbed answers 404 for a removed video (not for every
            # ended stream), so flag that case: it needs a person, and a problem makes GitHub send an email.
            status, _ = fetch(oembed(current))
            if status == 404:
                problems.append(f"{cam}: stream {current} is gone and its channel feed has no newer match")
            else:
                print(f"{cam}: OK ({current})" if hits else f"{cam}: stream older than the feed's 15 newest videos, keeping {current}")
        else:
            new = hits[0]
            status, _ = fetch(oembed(new))
            if status != 200:
                problems.append(f"{cam}: newest stream {new} can't be embedded (HTTP {status}), keeping {current}")
                continue
            page = page[:m.start(1)] + new + page[m.end(1):]
            changes.append(f"{cam}: {current} -> {new}")

    # Non-YouTube cams (IPCamLive, WetMet): can't heal, but flag one that stops allowing embedding.
    for cam, src in re.findall(r'\{ name: "([^"]+)", area: "[^"]+", src: "([^"]+)"', page):
        try:
            reason = embed_blocked(src)
        except RuntimeError as e:
            reason = f"player unreachable ({e})"
        if reason:
            problems.append(f"{cam}: {reason}. Make it a link-out tile (embed: false) or remove it")
        else:
            print(f"{cam}: embeddable")

    if changes:
        PAGE.write_text(page)
    report = ["## Stream check", *(f"- Updated {c}" for c in changes), *(f"- Problem: {p}" for p in problems)]
    if not changes and not problems:
        report.append("- All streams up to date.")
    print("\n".join(report))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text("\n".join(report) + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"problems={'1' if problems else '0'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
