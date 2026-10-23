#!/usr/bin/env python3
"""Render the award cards in awards.html from the Notion award database.

Cards are defined by data/award-cards.json (Notion page ID -> logo file);
every visible field comes from Notion. Any fetch or validation failure
exits non-zero before writing, so the last good awards.html stays live.
"""
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = ROOT / "awards.html"
CARDS_PATH = ROOT / "data" / "award-cards.json"
SNAPSHOT_PATH = ROOT / "awards-data.json"

DATABASE_ID = "245dd57cbe6180da961ecd6d239e066f"
NOTION_VERSION = "2022-06-28"
START, END = "<!-- AWARD-CARDS:START -->", "<!-- AWARD-CARDS:END -->"

P_TITLE = "獎項全名 (Title)"
P_COUNTRY = "國家"
P_CATS = "類別"
P_STATUS = "目前狀態"
P_YEAR = "資訊年度"
P_STAGE = "當前階段截止日"
P_FINAL = "最終截止日"
P_LINK = "官網連結"
P_FEE = "網站顯示費用"

COUNTRY_ORDER = ["德國", "日本", "台灣", "美國", "加拿大", "法國", "英國", "荷蘭", "歐洲", "芬蘭", "瑞士",
                 "義大利", "波蘭", "新加坡", "韓國", "香港", "中國", "亞洲", "澳洲", "國際"]
STATUS_CLASS = {"報名中": "st-open", "即將開放": "st-soon", "已截止": "st-closed", "待確認": "st-tbd"}
OPEN_STATUSES = {"報名中", "即將開放"}
TAIPEI = ZoneInfo("Asia/Taipei")
MISSING = "未提供"


class SyncError(Exception):
    pass


# ---------- Notion ----------

def notion_request(token, path, body):
    req = urllib.request.Request(
        f"https://api.notion.com/v1/{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(float(e.headers.get("Retry-After") or 2 ** attempt * 2))
                continue
            raise SyncError(f"Notion {path}: HTTP {e.code} {e.read().decode(errors='replace')[:300]}")
        except urllib.error.URLError as e:
            if attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            raise SyncError(f"Notion {path}: {e.reason}")


def fetch_pages(token):
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = notion_request(token, f"databases/{DATABASE_ID}/query", body)
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            return pages
        cursor = data.get("next_cursor")
        time.sleep(0.35)


def text_of(prop):
    if not prop:
        return ""
    items = prop.get("title") or prop.get("rich_text") or []
    return "".join(i.get("plain_text", "") for i in items).strip()


def normalize(page):
    props = page.get("properties", {})
    return {
        "notion_id": page["id"].replace("-", ""),
        "name": text_of(props.get(P_TITLE)),
        "country": ((props.get(P_COUNTRY) or {}).get("select") or {}).get("name", ""),
        "categories": [o["name"] for o in (props.get(P_CATS) or {}).get("multi_select", [])],
        "status": [o["name"] for o in (props.get(P_STATUS) or {}).get("multi_select", [])],
        "year": text_of(props.get(P_YEAR)),
        "stage_deadline": (props.get(P_STAGE) or {}).get("date"),
        "final_deadline": (props.get(P_FINAL) or {}).get("date"),
        "link": (props.get(P_LINK) or {}).get("url") or "",
        "fee": text_of(props.get(P_FEE)),
        "last_edited": page.get("last_edited_time", ""),
    }


# ---------- deadlines ----------

def utc_label(offset):
    minutes = int(offset.total_seconds() // 60)
    if minutes == 480:
        return "台灣時間"
    sign = "+" if minutes >= 0 else "−"
    h, m = divmod(abs(minutes), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")


def parse_deadline(d):
    """Return a deadline dict, or None. Ranges use their end date."""
    if not d or not d.get("start"):
        return None
    raw = d.get("end") or d["start"]
    tz_name = d.get("time_zone")
    if "T" not in raw and " " not in raw:
        day = date.fromisoformat(raw[:10])
        # date-only: no time zone is assumed; it counts as passed once that
        # calendar day has ended everywhere (UTC-12)
        ends = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc) + timedelta(days=1)
        return {"raw": raw, "has_time": False, "display": f"{day.year}/{day.month}/{day.day}",
                "sort": raw[:10], "ends_at": ends}
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name) if tz_name else timezone.utc)
    if tz_name:
        local = dt.astimezone(ZoneInfo(tz_name))
    elif dt.utcoffset() == timedelta(0):
        local = dt.astimezone(TAIPEI)
    else:
        local = dt
    shown = f"{local.year}/{local.month}/{local.day} {local:%H:%M}（{utc_label(local.utcoffset())}）"
    return {"raw": raw, "has_time": True, "display": shown,
            "sort": dt.astimezone(timezone.utc).isoformat(), "ends_at": dt}


def deadline_rows(rec):
    stage, final = parse_deadline(rec["stage_deadline"]), parse_deadline(rec["final_deadline"])
    open_like = bool(OPEN_STATUSES & set(rec["status"]))
    rows = []
    if stage and open_like:
        rows.append(("當前階段截止", stage))
    if final and not (rows and rows[0][1]["raw"] == final["raw"]):
        rows.append(("最終截止", final))
    if not rows and stage:
        rows.append(("截止", stage))
    return rows


# ---------- rendering ----------

def esc(s):
    return html.escape(s, quote=True)


def split_name(name):
    lines = [l.strip() for l in name.splitlines() if l.strip()]
    if len(lines) > 1:
        return lines[-1], " ".join(lines[:-1])
    return (lines[0] if lines else ""), ""


def is_dark(hex_color):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b < 140


def render_card(num, rec, card):
    primary, secondary = split_name(rec["name"])
    logo_path = ROOT / card["logo"]
    version = hashlib.sha1(logo_path.read_bytes()).hexdigest()[:8]
    bg = card["logo_bg"]
    link = rec["link"] if re.match(r"https?://", rec["link"]) else ""
    tag = "a" if link else "div"
    href = f' href="{esc(link)}" target="_blank" rel="noopener"' if link else ""
    statuses = rec["status"] or [MISSING]
    status_html = "".join(
        f'<span class="card-status {STATUS_CLASS.get(s, "st-tbd")}">{esc(s)}</span>' for s in statuses)
    rows = deadline_rows(rec)
    if rows:
        deadline_html = "".join(
            f'      <div class="card-meta-item"><span class="meta-label">{label}</span>'
            f'<span class="meta-val" data-deadline="{esc(d["sort"])}">{esc(d["display"])}</span></div>\n'
            for label, d in rows)
    else:
        deadline_html = ('      <div class="card-meta-item"><span class="meta-label">截止</span>'
                         '<span class="meta-val is-missing">未公布</span></div>\n')
    fee = rec["fee"]
    fee_html = esc(fee) if fee else MISSING
    search = " ".join([rec["name"], rec["country"], *rec["categories"], *rec["status"]]).split()
    search = " ".join(search).lower()
    return (
        f'<{tag} class="card" data-notion-id="{rec["notion_id"]}"{href}\n'
        f'   data-country="{esc(rec["country"])}" data-cats="{esc("|".join(rec["categories"]))}"'
        f' data-status="{esc("|".join(rec["status"]))}" data-search="{esc(search)}">\n'
        f'  <div class="card-num">{num:02d}</div>\n'
        f'  <div class="card-logo-wrap{" is-dark" if is_dark(bg) else ""}" style="background:{bg}">'
        f'<img src="{esc(card["logo"])}?v={version}" alt="{esc(primary)}" loading="lazy" decoding="async"></div>\n'
        f'  <div class="card-info">\n'
        f'    <div class="card-tags"><span class="card-country">{esc(rec["country"] or MISSING)}</span>{status_html}</div>\n'
        f'    <div class="card-names">\n'
        f'      <div class="card-name-en">{esc(primary)}</div>\n'
        + (f'      <div class="card-name-zh">{esc(secondary)}</div>\n' if secondary else "")
        + f'    </div>\n'
        f'    <div class="card-meta">\n'
        f'      <div class="card-meta-item"><span class="meta-label">資訊年度</span>'
        f'<span class="meta-val{"" if rec["year"] else " is-missing"}">{esc(rec["year"] or MISSING)}</span></div>\n'
        f'{deadline_html}'
        f'      <div class="card-meta-item is-block"><span class="meta-label">網站顯示費用</span>'
        f'<span class="meta-val card-fee{"" if fee else " is-missing"}">{fee_html}</span></div>\n'
        f'    </div>\n'
        f'  </div>\n'
        + ('  <div class="card-arrow">→</div>\n' if link else "")
        + f'</{tag}>'
    )


def find_issues(rec, now):
    issues = []
    name = rec["name"].splitlines()[0]
    if not rec["status"]:
        issues.append(f"{name}：目前狀態空白")
    if not rec["fee"]:
        issues.append(f"{name}：網站顯示費用空白")
    if not rec["year"]:
        issues.append(f"{name}：資訊年度空白")
    stage, final = parse_deadline(rec["stage_deadline"]), parse_deadline(rec["final_deadline"])
    if not stage and not final:
        issues.append(f"{name}：沒有任何截止日")
    if "報名中" in rec["status"]:
        current = stage or final
        if current and current["ends_at"] <= now:
            issues.append(f"{name}：狀態為報名中，但{'當前階段' if stage else '最終'}截止日 {current['display']} 已過")
        if not stage:
            issues.append(f"{name}：狀態為報名中，但當前階段截止日空白")
    if "已截止" in rec["status"] and final and final["ends_at"] > now:
        issues.append(f"{name}：狀態為已截止，但最終截止日 {final['display']} 尚未到")
    if stage and final and stage["ends_at"] > final["ends_at"]:
        issues.append(f"{name}：當前階段截止日 {stage['display']} 晚於最終截止日 {final['display']}")
    if "矛盾" in rec["year"]:
        issues.append(f"{name}：資訊年度註記「{rec['year']}」")
    return issues


def build(records, cards, source_html, now):
    by_id = {}
    for rec in records:
        if not rec["name"]:
            continue
        by_id.setdefault(rec["notion_id"], rec)

    seen, missing_ids, ordered = set(), [], []
    for index, card in enumerate(cards):
        nid = card["notion_id"]
        if nid in seen:
            raise SyncError(f"duplicate card for Notion page {nid} in {CARDS_PATH.name}")
        seen.add(nid)
        if not (ROOT / card["logo"]).is_file():
            raise SyncError(f"logo file missing: {card['logo']}")
        if nid not in by_id:
            missing_ids.append(nid)
            continue
        rec = by_id[nid]
        country_rank = COUNTRY_ORDER.index(rec["country"]) if rec["country"] in COUNTRY_ORDER else len(COUNTRY_ORDER)
        ordered.append((country_rank, index, rec, card))

    if len(missing_ids) > max(2, len(cards) * 0.1):
        raise SyncError(f"{len(missing_ids)} cards have no matching Notion page; check integration access")
    for nid in missing_ids:
        print(f"WARNING: card {nid} dropped: page not found in Notion", file=sys.stderr)

    ordered.sort(key=lambda t: (t[0], t[1]))
    grid = "\n".join(render_card(i + 1, rec, card) for i, (_, _, rec, card) in enumerate(ordered))

    if source_html.count(START) != 1 or source_html.count(END) != 1:
        raise SyncError("awards.html must contain exactly one AWARD-CARDS START/END marker pair")
    head, rest = source_html.split(START)
    _, tail = rest.split(END)
    updated = f"{head}{START}\n{grid}\n{END}{tail}"
    updated, n = re.subn(r'(<span class="result-count" id="resultCount">)[^<]*(</span>)',
                         rf"\g<1>共 {len(ordered)} 個獎項\g<2>", updated)
    if n != 1:
        raise SyncError("result count element not found in awards.html")

    card_ids = {rec["notion_id"] for _, _, rec, _ in ordered}
    issues = [i for _, _, rec, _ in ordered for i in find_issues(rec, now)]
    public = lambda rec: {k: rec[k] for k in ("notion_id", "name", "country", "categories", "status", "year",
                                             "stage_deadline", "final_deadline", "link", "fee")}
    snapshot = {
        "notion_valid_records": len(by_id),
        "notion_blank_name_records": sum(1 for r in records if not r["name"]),
        "card_count": len(ordered),
        "cards": [dict(public(rec), logo=card["logo"]) for _, _, rec, card in ordered],
        "records_without_logo": sorted((public(r) for nid, r in by_id.items() if nid not in card_ids),
                                       key=lambda r: (r["country"], r["name"])),
        "data_issues": issues,
    }
    return updated, snapshot


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    if "--records" in args:
        records = json.loads(Path(args[args.index("--records") + 1]).read_text(encoding="utf-8"))
    else:
        token = os.environ.get("NOTION_TOKEN")
        if not token:
            sys.exit("NOTION_TOKEN is not set")
        pages = [p for p in fetch_pages(token) if not p.get("archived") and not p.get("in_trash")]
        records = [normalize(p) for p in pages]
    if not records:
        raise SyncError("Notion returned no records")

    cards = json.loads(CARDS_PATH.read_text(encoding="utf-8"))
    source = HTML_PATH.read_text(encoding="utf-8")
    updated, snapshot = build(records, cards, source, datetime.now(timezone.utc))

    print(f"Notion valid records: {snapshot['notion_valid_records']} "
          f"(blank names skipped: {snapshot['notion_blank_name_records']}) | cards rendered: {snapshot['card_count']} "
          f"| records without logo: {len(snapshot['records_without_logo'])}")
    for issue in snapshot["data_issues"]:
        print(f"DATA: {issue}")

    old = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8")) if SNAPSHOT_PATH.exists() else {}
    old.pop("synced_at", None)
    if updated == source and old == snapshot:
        print("No changes")
        return
    if dry_run:
        print("Changes detected (dry run, nothing written)")
        return

    tmp = HTML_PATH.with_suffix(".html.tmp")
    tmp.write_text(updated, encoding="utf-8")
    tmp.replace(HTML_PATH)
    snapshot = {"synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **snapshot}
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("awards.html and awards-data.json updated")


if __name__ == "__main__":
    try:
        main()
    except SyncError as e:
        sys.exit(f"Sync failed, awards.html left unchanged: {e}")
