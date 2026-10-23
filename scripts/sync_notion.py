#!/usr/bin/env python3
"""Sync award deadlines and fees from Notion into awards.html.

Any fetch or validation failure exits non-zero before writing, so the
last successfully synced awards.html stays live.
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = ROOT / "awards.html"
SNAPSHOT_PATH = ROOT / "awards-data.json"
NOTION_VERSION = "2022-06-28"
DEADLINE_PROP = "最終截止日"
FEE_PROP = "網站顯示費用"
EMPTY_DEADLINE = "待公布"

CARD_RE = re.compile(r'<a class="card" data-notion-id="([0-9a-f]{32})".*?</a>', re.S)


def fetch_page(token, page_id, retries=3):
    req = urllib.request.Request(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION},
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise RuntimeError(f"Notion page {page_id}: HTTP {e.code} {e.read().decode(errors='replace')[:200]}")
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise RuntimeError(f"Notion page {page_id}: {e.reason}")


def plain_text(prop):
    if not prop:
        return ""
    items = prop.get("rich_text") or prop.get("title") or []
    return "".join(i.get("plain_text", "") for i in items).strip()


def format_deadline(prop):
    date = (prop or {}).get("date") or {}
    start = date.get("start")
    if not start:
        return EMPTY_DEADLINE
    y, m, d = start[:10].split("-")
    return f"{int(y)}/{int(m)}/{int(d)}"


def replace_field(block, field, value):
    pattern = re.compile(rf'(<span class="meta-val" data-field="{field}">)(.*?)(</span>)', re.S)
    if not pattern.search(block):
        raise RuntimeError(f"card is missing data-field=\"{field}\"")
    return pattern.sub(lambda m: m.group(1) + value + m.group(3), block, count=1)


def current_field(block, field):
    m = re.search(rf'<span class="meta-val" data-field="{field}">(.*?)</span>', block, re.S)
    return m.group(1) if m else None


def main():
    dry_run = "--dry-run" in sys.argv
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        sys.exit("NOTION_TOKEN is not set")

    source = HTML_PATH.read_text(encoding="utf-8")
    page_ids = CARD_RE.findall(source)
    if not page_ids:
        sys.exit("No cards with data-notion-id found in awards.html")

    pages = {}
    for pid in dict.fromkeys(page_ids):
        page = fetch_page(token, pid)
        if page.get("archived") or page.get("in_trash"):
            raise RuntimeError(f"Notion page {pid} is archived; remove or remap its card")
        pages[pid] = page
        time.sleep(0.35)

    snapshot, changes = [], []

    def update(match):
        pid = match.group(1)
        block = match.group(0)
        props = pages[pid].get("properties", {})
        name = plain_text(next((p for p in props.values() if p.get("type") == "title"), None))

        deadline = format_deadline(props.get(DEADLINE_PROP))
        fee_text = plain_text(props.get(FEE_PROP))
        fee = html.escape(fee_text, quote=False) if fee_text else current_field(block, "fee")

        for field, new in (("deadline", deadline), ("fee", fee)):
            old = current_field(block, field)
            if old != new:
                changes.append(f"{name.splitlines()[0]} {field}: {old} -> {new}")
            block = replace_field(block, field, new)

        snapshot.append({"notion_id": pid, "name": name, "deadline": deadline, "fee": fee})
        return block

    updated = CARD_RE.sub(update, source)

    empty = sum(1 for c in snapshot if c["deadline"] == EMPTY_DEADLINE)
    if empty > len(snapshot) * 0.8:
        sys.exit(f"Aborting: {empty}/{len(snapshot)} cards have no deadline; check the Notion data")

    for line in changes:
        print(line)
    print(f"{len(changes)} change(s) across {len(snapshot)} cards")

    if dry_run or not changes:
        return

    tmp = HTML_PATH.with_suffix(".html.tmp")
    tmp.write_text(updated, encoding="utf-8")
    tmp.replace(HTML_PATH)
    SNAPSHOT_PATH.write_text(
        json.dumps(
            {"synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "cards": snapshot},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        sys.exit(f"Sync failed, awards.html left unchanged: {e}")
