#!/usr/bin/env python3
"""
Okay, But... Birds — episode search index builder

Reads the LIVE episode pages on okaybutbirds.com (so the index always matches
what visitors see) and writes one JSON file the /episodes search loads.

  First run / after any page-layout change — check what gets extracted:
    python build_search_index.py --probe e39

  Weekly, after the new episode's transcript is on its page:
    python build_search_index.py --out ~/Projects/obb-search/obb-search-index.json --push

Requires: pip install requests beautifulsoup4
"""
import argparse
import json
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

SITE = "https://www.okaybutbirds.com"
DIRECTORY = "/episodes"
EPISODE_PATH = re.compile(r"^/episodes/(e\d+)/?$", re.I)

# If --probe shows a panel detected wrongly, set a CSS selector here and it wins.
TRANSCRIPT_SELECTOR = None   # e.g. "details.obb-transcript"
CREDITS_SELECTOR = None      # e.g. "details.obb-credits"

# Site chrome and anything repeated on every page. Pagination matters most:
# its prev/next links carry other episodes' titles and would cause false matches.
STRIP_SELECTORS = [
    "script", "style", "noscript", "svg", "template", "iframe",
    "header", "footer", "nav", "form",
    "#footer-sections", ".item-pagination", ".sqs-block-newsletter", ".newsletter-form-wrapper",
]

TIMESTAMP = re.compile(r"[\[(]?\b\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?\b[\])]?")
SITE_SUFFIX = re.compile(r"\s+[—–|-]\s+Okay,?\s+But.*$", re.I)
HEADERS = {"User-Agent": "OBB-search-indexer/1.0 (+https://www.okaybutbirds.com)"}


def clean(text, strip_timestamps=False):
    text = unicodedata.normalize("NFC", text or "")
    if strip_timestamps:
        text = TIMESTAMP.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch(session, url):
    r = session.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.content, "html.parser")  # bytes: let the page's own charset decide


def list_episodes(session, site):
    soup = fetch(session, urljoin(site, DIRECTORY))
    found = {}
    for a in soup.find_all("a", href=True):
        m = EPISODE_PATH.match(urlparse(urljoin(site, a["href"])).path)
        if m:
            found.setdefault(m.group(1).lower(), f"/episodes/{m.group(1).lower()}")
    return sorted(found.items(), key=lambda kv: int(kv[0][1:]))


def find_panel(root, word, selector):
    """Locate a collapsible panel (transcript/credits) without knowing the exact markup."""
    if selector:
        return root.select_one(selector)
    for d in root.find_all("details"):                       # 1. <details><summary>Transcript
        s = d.find("summary")
        if s and word in s.get_text(" ", strip=True).lower():
            return d
    for el in root.find_all(True):                            # 2. id/class mentions the word
        attrs = " ".join([el.get("id") or ""] + (el.get("class") or []))
        if word in attrs.lower():
            return el
    for el in root.find_all(["h1", "h2", "h3", "h4", "button", "summary", "strong", "p"]):
        t = el.get_text(" ", strip=True).lower()               # 3. a short label "Transcript"
        if t.startswith(word) and len(t) < 40:
            return el.find_parent(class_=re.compile(r"sqs-block|fe-block")) or el.parent
    return None


def panel_text(el, word, strip_timestamps=False):
    for label in el.find_all("summary"):
        label.decompose()
    text = clean(el.get_text(" "), strip_timestamps)
    return re.sub(rf"^{word}\w*\s*:?\s*", "", text, flags=re.I)


def extract(session, site, slug, path):
    soup = fetch(session, urljoin(site, path))

    title = ""
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = SITE_SUFFIX.sub("", og["content"])
    elif soup.title:
        title = SITE_SUFFIX.sub("", soup.title.get_text())

    root = soup.find("main") or soup.find(id="sections") or soup.body or soup
    for sel in STRIP_SELECTORS:
        for el in root.select(sel):
            el.decompose()

    credits_el = find_panel(root, "credit", CREDITS_SELECTOR)
    credits = panel_text(credits_el, "credit") if credits_el else ""
    if credits_el:
        credits_el.decompose()

    transcript_el = find_panel(root, "transcript", TRANSCRIPT_SELECTOR)
    transcript = panel_text(transcript_el, "transcript", strip_timestamps=True) if transcript_el else ""
    if transcript_el:
        transcript_el.decompose()

    notes = clean(root.get_text(" "))
    if title and notes.startswith(title):
        notes = notes[len(title):].strip()

    return {
        "id": slug,
        "url": path,
        "title": clean(title),
        "notes": notes,
        "credits": credits,
        "transcript": transcript,
    }


def git_push(out_path, count):
    repo = subprocess.run(["git", "-C", str(out_path.parent), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True)
    if repo.returncode != 0:
        sys.exit(f"--push: {out_path.parent} is not inside a git repository")
    top = repo.stdout.strip()
    subprocess.run(["git", "-C", top, "add", str(out_path)], check=True)
    if subprocess.run(["git", "-C", top, "diff", "--cached", "--quiet"]).returncode == 0:
        print("Index unchanged; nothing to push.")
        return
    subprocess.run(["git", "-C", top, "commit", "-m", f"Update search index ({count} episodes)"], check=True)
    # The GitHub Action in obb-search also commits the index, so catch up first.
    # A conflict on the index is harmless: both sides are full rebuilds of the same live site.
    pull = subprocess.run(["git", "-C", top, "pull", "--rebase", "-X", "theirs"], capture_output=True, text=True)
    if pull.returncode != 0:
        subprocess.run(["git", "-C", top, "rebase", "--abort"], capture_output=True)
        sys.exit("git pull --rebase failed; resolve in " + top + " and push by hand.\n" + pull.stderr)
    subprocess.run(["git", "-C", top, "push"], check=True)
    print("Pushed. GitHub Pages usually serves the new file within a few minutes.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", default=SITE)
    ap.add_argument("--out", help="where to write obb-search-index.json")
    ap.add_argument("--probe", metavar="SLUG", help="print what one episode page yields, write nothing")
    ap.add_argument("--push", action="store_true", help="git commit + push the repo containing --out")
    ap.add_argument("--force", action="store_true", help="write even if the index shrank")
    ap.add_argument("--delay", type=float, default=0.5, help="seconds between page requests")
    args = ap.parse_args()
    session = requests.Session()

    if args.probe:
        slug = args.probe.lower()
        ep = extract(session, args.site, slug, f"/episodes/{slug}")
        for field in ("title", "notes", "credits", "transcript"):
            val = ep[field]
            print(f"\n== {field} ({len(val):,} chars)")
            print((val[:300] + (" …" if len(val) > 300 else "")) or "(nothing found)")
        return

    if not args.out:
        ap.error("--out is required (or use --probe)")
    out = Path(args.out).expanduser().resolve()

    episodes = list_episodes(session, args.site)
    if not episodes:
        sys.exit("No episode links found on the directory page; nothing written.")

    index, missing = [], []
    for slug, path in episodes:
        try:
            ep = extract(session, args.site, slug, path)
        except Exception as e:  # any failure aborts: never publish a partial index
            sys.exit(f"Failed on {slug}: {e}\nNothing written.")
        if not ep["transcript"]:
            missing.append(slug)
        index.append(ep)
        print(f"  {slug:<4} {len(ep['transcript']):>7,} transcript chars  {ep['title'][:60]}")
        time.sleep(args.delay)

    before = {}
    if out.exists():
        try:
            before = json.loads(out.read_text())
        except Exception:
            before = {}
    if before.get("episodes") == index:
        print("\nNo content changes since the last build; file left as is.")
        if missing:
            print(f"No transcript found for: {', '.join(missing)}")
        return
    if before and not args.force:
        prev_n = len(before.get("episodes", []))
        prev_t = sum(1 for e in before.get("episodes", []) if e.get("transcript"))
        now_t = len(index) - len(missing)
        if len(index) < prev_n or now_t < prev_t:
            sys.exit(f"Index would shrink ({prev_n}→{len(index)} episodes, {prev_t}→{now_t} transcripts). "
                     "Check the site, or rerun with --force. Nothing written.")

    payload = {
        "v": 1,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "episodes": index,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    print(f"\nWrote {out} — {len(index)} episodes, {out.stat().st_size / 1024:,.0f} KB")
    if missing:
        print(f"No transcript found for: {', '.join(missing)}")

    if args.push:
        git_push(out, len(index))


if __name__ == "__main__":
    main()
