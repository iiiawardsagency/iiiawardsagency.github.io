#!/usr/bin/env python3
"""Render the award cards in awards.html from the Notion award database.

Cards are defined by data/award-cards.json (Notion page ID -> logo file);
every visible field comes from Notion. Any fetch or validation failure
exits non-zero before writing, so the last good awards.html stays live.

Awards that have confirmed rows in the stage database (獎項階段) get their
status, current stage, deadlines, fee and year worked out from those rows,
and the result is written back to the award database (only fields that
changed; a failed write is a warning, never a failed sync). Awards with no
confirmed stage rows keep using the award database fields as before.

Options: --dry-run (write nothing; print the Notion write-back plan),
--records FILE / --stages FILE (offline data instead of Notion),
--now ISO_DATETIME (pretend it is this time; for testing).
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
STAGE_DATABASE_ID = "424dd57cbe61819da471e068a0d2a4c2"
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

# stage database (獎項階段)
S_NAME = "階段"
S_AWARD = "獎項"
S_TYPE = "階段類型"
S_ORDER = "順序"
S_START = "開始日"
S_DEADLINE = "截止日"
S_FEE = "費用"
S_SEASON = "屆別年度"
S_REVIEW = "審核狀態"
REVIEW_OK, REVIEW_PENDING = "已確認", "待審核"

COUNTRY_ORDER = ["德國", "日本", "台灣", "美國", "加拿大", "法國", "英國", "荷蘭", "歐洲", "芬蘭", "瑞士",
                 "義大利", "波蘭", "新加坡", "韓國", "香港", "中國", "亞洲", "澳洲", "國際"]
STATUS_CLASS = {"報名中": "st-open", "即將開放": "st-soon", "已截止": "st-closed", "待確認": "st-tbd"}
OPEN_STATUSES = {"報名中", "即將開放"}
TAIPEI = ZoneInfo("Asia/Taipei")
MISSING = "未提供"


class SyncError(Exception):
    pass


# ---------- Notion ----------

def notion_request(token, path, body, method="POST"):
    req = urllib.request.Request(
        f"https://api.notion.com/v1/{path}",
        data=json.dumps(body).encode(),
        method=method,
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


def fetch_pages(token, database_id=DATABASE_ID):
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = notion_request(token, f"databases/{database_id}/query", body)
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


def normalize_stage(page):
    props = page.get("properties", {})
    order = (props.get(S_ORDER) or {}).get("number")
    return {
        "stage_id": page["id"].replace("-", ""),
        "name": text_of(props.get(S_NAME)),
        "award_ids": [r["id"].replace("-", "") for r in (props.get(S_AWARD) or {}).get("relation", [])],
        "type": ((props.get(S_TYPE) or {}).get("select") or {}).get("name", ""),
        "order": order,
        "start": (props.get(S_START) or {}).get("date"),
        "deadline": (props.get(S_DEADLINE) or {}).get("date"),
        "fee": text_of(props.get(S_FEE)),
        "season": text_of(props.get(S_SEASON)),
        "review": ((props.get(S_REVIEW) or {}).get("select") or {}).get("name", ""),
    }


# ---------- deadlines ----------

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
    # Cards show a date only, in Taiwan time. A deadline in the small hours (before
    # noon) shows the day before, which is the last full day to submit from Taiwan:
    # 12/2 05:00 shows 12/1, 12/2 23:00 shows 12/2, midnight 12/2 00:00 shows 12/1.
    local = dt.astimezone(TAIPEI)
    day = local.date() - timedelta(days=1) if local.hour < 12 else local.date()
    shown = f"{day.year}/{day.month}/{day.day}"
    return {"raw": raw, "has_time": True, "display": shown,
            "sort": dt.astimezone(timezone.utc).isoformat(), "ends_at": dt}


def shown_status(rec, now):
    """Status shown on the site. Notion's 目前狀態 is used as-is, except that an
    award still marked 報名中 / 即將開放 shows 已截止 once its final deadline has
    passed (a safety net for when Notion is not updated in time)."""
    final = parse_deadline(rec["final_deadline"])
    if final and final["ends_at"] <= now and OPEN_STATUSES & set(rec["status"]):
        return ["已截止"] + [s for s in rec["status"] if s not in OPEN_STATUSES and s != "已截止"]
    return rec["status"]


def deadline_rows(rec):
    if rec.get("current_stage"):
        return stage_deadline_rows(rec)
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


# ---------- stages (獎項階段) ----------

def parse_start(d):
    """When a stage opens, or None. A date without a time counts as open from the
    moment that calendar day begins anywhere (UTC+14), mirroring parse_deadline,
    which keeps a date-only deadline open until the day has ended everywhere."""
    if not d or not d.get("start"):
        return None
    raw = d["start"]
    if "T" not in raw and " " not in raw:
        day = date.fromisoformat(raw[:10])
        return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) - timedelta(hours=14)
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(d["time_zone"]) if d.get("time_zone") else timezone.utc)
    return dt


def stage_label(name):
    """「紅點產品｜一般」→「一般」. A name without ｜ is used whole."""
    for sep in ("｜", "|"):
        if sep in name:
            return name.split(sep, 1)[1].strip() or name.strip()
    return name.strip()


def label_with(text, suffix):
    """「早鳥」+「截止」→「早鳥截止」; 「Last Minute」+「截止」→「Last Minute 截止」;
    a generic or already-suffixed name (「截止」) is not doubled."""
    if not text or text in GENERIC_LABELS or text.endswith(suffix):
        return suffix if not text or text in GENERIC_LABELS else text
    return f"{text} {suffix}" if text[-1].isascii() and text[-1].isalnum() else f"{text}{suffix}"


FEE_PLACEHOLDER = re.compile(r"未列|未公布|未公佈|待確認|待官方|未提供|未定")
GENERIC_LABELS = {"截止", "報名", "報名期"}


def stage_fee_text(status, stage):
    """「早鳥：USD 370／件」. A fee the stage row marks as not yet published shows
    「費用未公布」; a generic stage name (「截止」) is left out of the fee."""
    label = stage_label(stage["name"])
    fee = stage["fee"].strip()
    if not fee or FEE_PLACEHOLDER.search(fee):
        fee = "費用未公布"
    elif label not in GENERIC_LABELS:
        fee = f"{label}：{fee}"
    return f"已截止｜{fee}" if status == "已截止" else fee


def season_year(season, fallback):
    """The 屆別年度 when it names a year (2027, 2026 S2, 2026–2027); otherwise
    (e.g. 「第 15 屆」) the award's own 資訊年度, so the card always shows a year."""
    return season if re.search(r"(19|20)\d\d", season or "") else (fallback or season)


def stage_state(stages, now):
    """Status and current stage from one award's confirmed stage rows, or None when
    no row has a usable deadline. Rows are ordered by deadline (順序 breaks ties)."""
    timed = [(parse_deadline(s["deadline"]), s) for s in stages]
    timed = [(d, s) for d, s in timed if d]
    if not timed:
        return None
    timed.sort(key=lambda t: (t[0]["ends_at"], t[1]["order"] if t[1]["order"] is not None else 0))
    ordered = [s for _, s in timed]
    current = next((s for d, s in timed if d["ends_at"] > now), None)
    if current is None:
        status, current = "已截止", ordered[-1]
    else:
        opens = parse_start(current["start"])
        status = "即將開放" if opens and now < opens else "報名中"
    return {"status": status, "current": current, "final": ordered[-1], "ordered": ordered}


def stage_record(rec, state):
    """The award record as the site should show it, with stage-derived fields."""
    current, final, status = state["current"], state["final"], state["status"]
    return dict(
        rec,
        status=[status],
        stage_deadline=None if status == "已截止" else current["deadline"],
        final_deadline=final["deadline"],
        fee=stage_fee_text(status, current),
        year=season_year(current["season"], rec["year"]),
        current_stage={k: current[k] for k in ("name", "type", "order", "start", "deadline", "fee", "season")},
        final_is_current=current is final,
    )


def stage_deadline_rows(rec):
    stage = rec["current_stage"]
    current = parse_deadline(stage["deadline"])
    rows = [(label_with(stage_label(stage["name"]), "截止"), current)]
    final = parse_deadline(rec["final_deadline"])
    if final and not rec["final_is_current"]:
        rows.append(("最終截止", final))
    return rows


def stage_issues(stages, records):
    """data_issues about the stage rows themselves."""
    names = {r["notion_id"]: r["name"].splitlines()[0] for r in records}
    issues, pending = [], {}
    for s in stages:
        label = s["name"] or s["stage_id"]
        if not s["award_ids"]:
            issues.append(f"階段「{label}」沒有連到任何獎項")
            continue
        if s["review"] == REVIEW_PENDING:
            for aid in s["award_ids"]:
                pending.setdefault(aid, []).append(label)
        if s["review"] == REVIEW_OK and not parse_deadline(s["deadline"]):
            issues.append(f"階段「{label}」已確認但沒有截止日，判斷時略過")
    for aid, labels in pending.items():
        issues.append(f"{names.get(aid, aid)}：有 {len(labels)} 個待審核階段（{'、'.join(labels)}）")
    return issues


def order_issue(name, state):
    """Flag an award whose 順序 does not follow its deadlines."""
    orders = [s["order"] for s in state["ordered"]]
    if None in orders or orders == sorted(orders):
        return None
    seq = "、".join(stage_label(s["name"]) for s in state["ordered"])
    return f"{name}：階段「順序」和截止日先後不一致（依截止日為 {seq}）"


# ---------- Notion write-back ----------

def instant_key(raw, tz_name):
    if not raw:
        return None
    if "T" not in raw and " " not in raw:
        return raw[:10]
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name) if tz_name else timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def date_key(d):
    if not d or not d.get("start"):
        return None
    return instant_key(d["start"], d.get("time_zone")), instant_key(d.get("end"), d.get("time_zone"))


def date_payload(d):
    if not d or not d.get("start"):
        return {"date": None}
    out = {"start": d["start"]}
    if d.get("end"):
        out["end"] = d["end"]
    if d.get("time_zone"):
        out["time_zone"] = d["time_zone"]
    return {"date": out}


def text_payload(s):
    return {"rich_text": [{"type": "text", "text": {"content": s}}] if s else []}


def writeback_plan(rec, shown):
    """Award-database properties that differ from the stage-derived values."""
    props, changes = {}, []
    if rec["status"] != shown["status"]:
        props[P_STATUS] = {"multi_select": [{"name": s} for s in shown["status"]]}
        changes.append((P_STATUS, "、".join(rec["status"]) or "（空白）", shown["status"][0]))
    for prop, key in ((P_STAGE, "stage_deadline"), (P_FINAL, "final_deadline")):
        if date_key(rec[key]) != date_key(shown[key]):
            props[prop] = date_payload(shown[key])
            changes.append((prop, (rec[key] or {}).get("start") or "（空白）", (shown[key] or {}).get("start") or "（清空）"))
    for prop, key in ((P_FEE, "fee"), (P_YEAR, "year")):
        if rec[key] != shown[key]:
            props[prop] = text_payload(shown[key])
            changes.append((prop, rec[key] or "（空白）", shown[key] or "（清空）"))
    return props, changes


def write_back(token, plans, dry_run):
    """Apply the write-back plans. Never raises: failures are warnings."""
    if not plans:
        print("Notion write-back: nothing to change")
        return
    print(f"Notion write-back: {len(plans)} award(s) {'would change (dry run)' if dry_run or not token else 'to update'}")
    failed = 0
    for i, (nid, name, props, changes) in enumerate(plans):
        for prop, old, new in changes:
            print(f"  {name}｜{prop}：{old} → {new}")
        if dry_run or not token:
            continue
        if i:
            time.sleep(0.4)  # stay under Notion's ~3 requests/second
        try:
            notion_request(token, f"pages/{nid}", {"properties": props}, method="PATCH")
        except SyncError as e:
            failed += 1
            print(f"WARNING: write-back failed for {name}: {e}", file=sys.stderr)
        except Exception as e:  # never let write-back break the site update
            failed += 1
            print(f"WARNING: write-back failed for {name}: {e!r}", file=sys.stderr)
    if not (dry_run or not token):
        print(f"Notion write-back done: {len(plans) - failed} updated, {failed} failed")


# ---------- rendering ----------

def esc(s):
    return html.escape(s, quote=True)


def clean_name_lines(name):
    lines = [l.strip() for l in name.splitlines() if l.strip()]
    # a line that is just two other lines run together is a paste duplicate
    joined = {a + b for a in lines for b in lines if a is not b}
    lines = [l for l in lines if l not in joined]
    lines = list(dict.fromkeys(lines))
    unwrapped = []
    for l in lines:
        if len(l) > 2 and l[0] in "（(" and l[-1] in "）)":
            l = l[1:-1].strip()
        unwrapped.append(l)
    return unwrapped


def split_name(name):
    lines = clean_name_lines(name)
    if len(lines) > 1:
        return lines[-1], " ".join(lines[:-1])
    return (lines[0] if lines else ""), ""


def is_dark(hex_color):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b < 140


def render_card(num, rec, card, now):
    if not rec.get("current_stage"):
        rec = dict(rec, status=shown_status(rec, now))
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
        f'  <div class="card-num" aria-hidden="true">{num:02d}</div>\n'
        f'  <div class="card-logo-wrap{" is-dark" if is_dark(bg) else ""}" style="background:{bg}">'
        f'<img src="{esc(card["logo"])}?v={version}" alt="{esc(primary)}" width="{card["w"]}" height="{card["h"]}" loading="lazy" decoding="async"></div>\n'
        f'  <div class="card-info">\n'
        f'    <div class="card-tags"><span class="card-country">{esc(rec["country"] or MISSING)}</span>{status_html}</div>\n'
        f'    <div class="card-names">\n'
        f'      <h3 class="card-name-en">{esc(primary)}</h3>\n'
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


def build(records, cards, source_html, now, stages=()):
    by_id = {}
    for rec in records:
        if not rec["name"]:
            continue
        by_id.setdefault(rec["notion_id"], rec)

    # Awards with confirmed stage rows: derive what to show, and what to write back.
    confirmed = {}
    for s in stages:
        if s["review"] == REVIEW_OK:
            for aid in s["award_ids"]:
                confirmed.setdefault(aid, []).append(s)
    shown, plans, stage_notes = {}, [], stage_issues(stages, records)
    for nid, rec in by_id.items():
        state = stage_state(confirmed.get(nid, []), now)
        if not state:
            shown[nid] = rec
            continue
        shown[nid] = view = stage_record(rec, state)
        name = rec["name"].splitlines()[0]
        props, changes = writeback_plan(rec, view)
        if props:
            plans.append((nid, name, props, changes))
        note = order_issue(name, state)
        if note:
            stage_notes.append(note)

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
        rec = shown[nid]
        country_rank = COUNTRY_ORDER.index(rec["country"]) if rec["country"] in COUNTRY_ORDER else len(COUNTRY_ORDER)
        ordered.append((country_rank, index, rec, card))

    if len(missing_ids) > max(2, len(cards) * 0.1):
        raise SyncError(f"{len(missing_ids)} cards have no matching Notion page; check integration access")
    for nid in missing_ids:
        print(f"WARNING: card {nid} dropped: page not found in Notion", file=sys.stderr)

    ordered.sort(key=lambda t: (t[0], t[1]))
    grid = "\n".join(render_card(i + 1, rec, card, now) for i, (_, _, rec, card) in enumerate(ordered))

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
    issues = [i for _, _, rec, _ in ordered for i in find_issues(rec, now)] + stage_notes
    public = lambda rec: {k: rec[k] for k in ("notion_id", "name", "country", "categories", "status", "year",
                                             "stage_deadline", "final_deadline", "link", "fee")}
    snapshot = {
        "notion_valid_records": len(by_id),
        "notion_blank_name_records": sum(1 for r in records if not r["name"]),
        "card_count": len(ordered),
        "cards": [dict(public(rec), logo=card["logo"], current_stage=rec.get("current_stage"))
                  for _, _, rec, card in ordered],
        "records_without_logo": sorted((public(r) for nid, r in shown.items() if nid not in card_ids),
                                       key=lambda r: (r["country"], r["name"])),
        "data_issues": issues,
    }
    return updated, snapshot, plans


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    opt = lambda name: args[args.index(name) + 1] if name in args else None
    token = None
    if opt("--records"):
        records = json.loads(Path(opt("--records")).read_text(encoding="utf-8"))
        stages = json.loads(Path(opt("--stages")).read_text(encoding="utf-8")) if opt("--stages") else []
    else:
        token = os.environ.get("NOTION_TOKEN")
        if not token:
            sys.exit("NOTION_TOKEN is not set")
        live = lambda ps: [p for p in ps if not p.get("archived") and not p.get("in_trash")]
        records = [normalize(p) for p in live(fetch_pages(token))]
        # A stage-database failure stops the sync like any other fetch failure, so
        # awards never flip between stage-based and old-field logic.
        stages = [normalize_stage(p) for p in live(fetch_pages(token, STAGE_DATABASE_ID))]
    if not records:
        raise SyncError("Notion returned no records")
    now = datetime.fromisoformat(opt("--now")) if opt("--now") else datetime.now(timezone.utc)

    cards = json.loads(CARDS_PATH.read_text(encoding="utf-8"))
    source = HTML_PATH.read_text(encoding="utf-8")
    updated, snapshot, plans = build(records, cards, source, now, stages)

    print(f"Notion valid records: {snapshot['notion_valid_records']} "
          f"(blank names skipped: {snapshot['notion_blank_name_records']}) | cards rendered: {snapshot['card_count']} "
          f"| records without logo: {len(snapshot['records_without_logo'])}")
    for issue in snapshot["data_issues"]:
        print(f"DATA: {issue}")
    write_back(token, plans, dry_run)

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
