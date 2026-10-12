"""Combine the pulled data with the reviewed notes and write the dashboard page."""
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CURATED, RAW, ROOT, load, now_stamp

SITE = ROOT / "site"      # page templates
DIST = ROOT / "dist"      # built pages, ready to publish
INTERCHANGE = "https://interchange.puc.texas.gov"
RECENT_DAYS = 45


def docket_url(control):
    return f"{INTERCHANGE}/search/filings/?ControlNumber={control}"


def filing_url(control, item):
    return f"{INTERCHANGE}/search/documents/?controlNumber={control}&itemNumber={item}"


# ---------- relevance ----------

def classify(item, rules, reviewed, footprint):
    """Return (tier, reason, basis) for one agenda item."""
    if item["control"] in reviewed:
        entry = reviewed[item["control"]]
        return entry["tier"], entry["reason"], "Reviewed"
    for rule in rules:
        when = rule["when"]
        if "section" in when and when["section"] != item["section"]:
            continue
        if "subsection" in when and when["subsection"] != item["subsection"]:
            continue
        if "title" in when and not re.search(when["title"], item["title"], re.I):
            continue
        if when.get("imm_has_filed") and item["control"] not in footprint:
            continue
        return rule["tier"], rule["reason"], "Rule"
    return "watch", "Not matched by any rule.", "Rule"


def filing_kind(filing):
    party, text, kind = filing["party"], filing["description"], filing["type"].strip()
    official = bool(re.search(r"^PUC\b|^MARKET ANALYSIS|^RULES & PROJECTS|EXECUTIVE DIRECTOR", party, re.I))
    if re.search(r"potomac", party, re.I):
        return "IMM filing"
    if kind == "PC":
        return "Public comment"
    if official and re.search(r"\b(CHAIRMAN|COMMISSIONER)\b.*MEMO", text, re.I):
        return "Commissioner memo"
    if official and re.search(r"^ORDER\b(?! NO\.)|\bORDER (GRANTING|ADOPTING|APPROVING|DENYING)\b", text, re.I) and not re.search(r"PROPOSED", text, re.I):
        return "Commission order"
    if official and re.search(r"memo|staff'?s? (response|request|recommend)|OM Item|Recommended|Proposal for|Proposed Order|Rulemaking Calendar", text, re.I):
        return "Staff memo"
    if official:
        return "Commission filing"
    if re.fullmatch(r"ERCOT", party.strip(), re.I) or re.search(r"Electric Reliability Council", party, re.I):
        return "ERCOT filing"
    return "Stakeholder filing"


SOURCE_NAMES = {
    "puct_meetings": "PUCT open meeting agendas",
    "puct_filings": "PUCT filing index (watched projects)",
    "puct_memos": "PUCT commissioner memo search",
    "documents": "Filed documents, full text",
    "ercot_nprr": "ERCOT revision requests",
    "ercot_calendar": "ERCOT meeting calendar",
    "tlo": "Texas Legislature Online",
}


def what_changed(before, now):
    """Plain-language list of what differs from the previous pull."""
    out = []
    for d in sorted(set(now["agendas"]) - set(before.get("agendas", []))):
        out.append(f"Agenda posted for the {datetime.strptime(d, '%Y-%m-%d').strftime('%B %-d')} open meeting")
    for d in sorted(set(now["outcomes"]) - set(before.get("outcomes", []))):
        out.append(f"Outcome lists posted for the {datetime.strptime(d, '%Y-%m-%d').strftime('%B %-d')} open meeting")
    grew = []
    for control, top in now["filings"].items():
        if control not in before.get("filings", {}):
            out.append(f"Now watching Project {control}")
        elif top > before["filings"][control]:
            grew.append((top - before["filings"][control], control))
    if grew:
        total = sum(n for n, _ in grew)
        where = ", ".join(f"{c} ({n})" for n, c in sorted(grew, reverse=True)[:4])
        out.append(f"{total} new filing{'s' if total != 1 else ''} in watched projects: {where}")
    old_nprr = before.get("nprr", {})
    for rid, group in now["nprr"].items():
        if rid not in old_nprr:
            out.append(f"New ERCOT revision request: {rid}")
        elif old_nprr[rid] != group:
            out.append(f"{rid} moved from {old_nprr[rid]} to {group}")
    gone = sorted(set(old_nprr) - set(now["nprr"]))
    if gone:
        out.append("No longer pending at ERCOT: " + ", ".join(gone[:8]) + ("…" if len(gone) > 8 else ""))
    for key, label in (("mentions", "filing{} that refer{} to the IMM"), ("memos", "commissioner memo{}")):
        fresh = len(set(now[key]) - set(before.get(key, [])))
        if fresh:
            out.append(f"{fresh} new " + label.format("s" if fresh != 1 else "", "" if fresh != 1 else "s"))
    return out


def main():
    today = date.today().isoformat()
    meetings_raw = load("meetings.json") or {"meetings": []}
    filings_raw = load("filings.json") or {"projects": {}}
    footprint_raw = load("imm_footprint.json") or {"projects": []}
    mentions_raw = load("mentions.json") or {"mentions": []}
    opinions_raw = load("board_opinions.json") or {"opinions": []}
    nprr_raw = load("nprr.json") or {"nprrs": []}
    ercot_cal = load("ercot_calendar.json") or {"events": []}
    legislature = load("legislature.json") or {"hearings": [], "bills": [], "feed_status": {}}

    rules = load("relevance_rules.json", CURATED)["rules"]
    judgments = load("agenda_judgments.json", CURATED)
    recs = load("recommendations.json", CURATED)
    actions = load("actions.json", CURATED)["actions"]
    extra_dates = load("calendar.json", CURATED)["events"]
    stances = load("mention_stances.json", CURATED)["stances"]
    nprr_notes = load("nprr_notes.json", CURATED)["notes"]
    site = load("site.json", CURATED)

    projects = filings_raw["projects"]
    footprint = {p["control"]: p for p in footprint_raw["projects"]}
    for filings in projects.values():
        for f in filings["filings"]:
            f["kind"] = filing_kind(f)

    def imm_filings(control):
        return [f for f in projects.get(control, {}).get("filings", []) if f["kind"] == "IMM filing"
                and not re.search(r"void", f["description"], re.I)]

    # ---------- meetings ----------
    parsed = [m for m in meetings_raw["meetings"] if m.get("items")]
    meetings = []
    for index, m in enumerate(parsed):
        upcoming = m["date"] >= today
        previous = parsed[index + 1]["date"] if index + 1 < len(parsed) else None
        items = []
        for raw in m["items"]:
            item = dict(raw)
            tier, reason, basis = classify(item, rules, judgments["by_control"], footprint)
            note = judgments["items"].get(f"{m['date']}#{item['num']}", {})
            item.update(tier=tier, reason=reason, basis=basis, summary=note.get("summary", ""), action=note.get("action", ""),
                        note_after=bool(note.get("after")))
            control = item["control"]
            if control:
                item["docket_url"] = docket_url(control)
            project = projects.get(control)
            if project and tier != "none":
                since = previous or m["date"]
                item["new_filings"] = [f for f in project["filings"] if f["date"] and since < f["date"] <= m["date"]
                                       and f["kind"] != "Public comment"][-8:]
                item["imm_filings"] = imm_filings(control)[-3:]
            if not upcoming:
                if item["num"] in m.get("consent", []):
                    item["outcome"] = "Taken up without discussion"
                elif item["num"] in m.get("not_taken_up", []):
                    item["outcome"] = "Not taken up"
                else:
                    item["outcome"] = "Taken up"
                    if project:
                        window_end = (datetime.strptime(m["date"], "%Y-%m-%d") + timedelta(days=4)).strftime("%Y-%m-%d")
                        orders = [f for f in project["filings"] if f["kind"] == "Commission order"
                                  and f["date"] and m["date"] <= f["date"] <= window_end]
                        if orders:
                            item["order"] = orders[-1]
            items.append(item)
        # How many straight earlier meetings skipped this docket.
        for item in items:
            if not item["control"]:
                continue
            streak = 0
            for earlier in parsed[index + 1:]:
                match = [i for i in earlier["items"] if i["control"] == item["control"]]
                if match and match[0]["num"] in earlier.get("not_taken_up", []):
                    streak += 1
                else:
                    break
            if streak:
                item["deferred_streak"] = streak
        meetings.append({
            "date": m["date"], "agenda_url": m["agenda_url"], "video_url": m["video_url"], "upcoming": upcoming,
            "items": items,
            "counts": {t: sum(1 for i in items if i["tier"] == t) for t in ("action", "watch", "none")},
        })
    archive = [{"date": m["date"], "agenda_url": m["agenda_url"], "video_url": m["video_url"]}
               for m in meetings_raw["meetings"] if not m.get("items")][:12]

    # ---------- recent filings and memo alerts ----------
    cutoff = (date.today() - timedelta(days=RECENT_DAYS)).isoformat()
    recent = []
    for control, project in projects.items():
        for f in project["filings"]:
            if f["date"] and f["date"] >= cutoff and f["kind"] != "Public comment":
                recent.append({**f, "control": control, "style": project["style"]})
    recent.sort(key=lambda f: (f["date"], f["control"], f["item"]), reverse=True)

    watched = []
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    for control, project in sorted(projects.items()):
        dated = [f for f in project["filings"] if f["date"]]
        watched.append({
            "control": control, "style": project["style"], "url": docket_url(control),
            "filings": len(project["filings"]),
            "last_filing": max((f["date"] for f in dated), default=""),
            "recent": sum(1 for f in dated if f["date"] >= cutoff and f["kind"] != "Public comment"),
            "imm_filings": len(imm_filings(control)),
            "last_imm_filing": max((f["date"] for f in imm_filings(control)), default=""),
            "in_footprint": control in footprint,
        })

    # ---------- commissioner memos (all dockets) ----------
    memos_raw = load("commissioner_memos.json") or {"memos": []}
    agenda_tier = {}
    for m in reversed(meetings):
        for i in m["items"]:
            if i["control"]:
                agenda_tier[i["control"]] = i["tier"]
    memos = []
    for memo in memos_raw["memos"]:
        tier = agenda_tier.get(memo["control"]) or classify(
            {"control": memo["control"], "title": memo["style"], "section": "", "subsection": ""},
            rules, judgments["by_control"], footprint)[0]
        memos.append({**memo, "tier": tier})

    # ---------- mentions ----------
    MENTION = re.compile(r"(?-i:\bIMM\b)|Independent Market Monitor|Potomac Economics|market monitor", re.I)
    says = re.compile(r"(IMM|Independent Market Monitor|Potomac Economics|market monitor)\W{0,3}(?:\([^)]*\)\W{0,2})?[^.]{0,60}?\b(has|have|had|is|was|recommend\w*|stat\w+|not\w+|report\w*|propos\w+|support\w*|oppos\w+|estimat\w+|argu\w+|identif\w+|request\w+|review|analysis|shows?)\b", re.I)
    citation = re.compile(r"https?:|www\.|\bat \d|\bSee\b|Item No|Project No|\(Aug|\(Sept|\(May|\(June", re.I)

    def trim(text):
        """Start the quote near the reference, on a word boundary."""
        found = MENTION.search(text)
        if found and found.start() > 170:
            text = text[found.start() - 150:]
            return "… " + text.split(" ", 1)[-1]
        if text and not (text[0].isupper() or text[0].isdigit() or text[0] in "\"“("):
            return "… " + text.split(" ", 1)[-1]
        return text

    def best_quotes(snippets, prefer=""):
        scored = []
        for order, snip in enumerate(snippets):
            text = trim(snip["text"])
            score = (2 if says.search(text) else 0) - (2 if citation.search(text) else 0) - (1 if len(text) < 90 else 0)
            if prefer and prefer.lower() in text.lower():
                score += 5
            scored.append((-score, order, {"page": snip["page"], "text": text}))
        scored.sort(key=lambda x: x[:2])
        good = []
        for score, _, snip in scored:
            if -score >= 1 and not any(snip["text"][:80] == kept["text"][:80] for kept in good):
                good.append(snip)
        if good:
            return good[:2]
        return [snip for _, _, snip in scored[:1] if len(snip["text"]) >= 90]

    mentions = []
    for m in mentions_raw["mentions"]:
        review = stances.get(f"{m['control']}#{m['item']}", {})
        entry = {k: v for k, v in m.items() if k != "snippets"}
        mentions.append({**entry, "quotes": best_quotes(m["snippets"], review.get("prefer", "")), "kind": filing_kind(m),
                         "stance": review.get("stance", "Not yet read"), "note": review.get("note", "")})
    mentions.sort(key=lambda m: (m["date"], m["control"], m["item"]), reverse=True)

    # ---------- ERCOT rule changes ----------
    opinions = {o["request"]: o for o in opinions_raw["opinions"]}
    stage_order = {"PRS": 1, "WMS": 1, "ROS": 1, "TAC": 2, "BOARD": 3, "Board": 3, "PUCT": 4}
    nprrs = []
    for n in nprr_raw["nprrs"]:
        if "error" in n:
            continue
        opinion = opinions.get(n["id"])
        commented = bool(n["imm_documents"])
        supports = bool(opinion and opinion["position"] == "Supports")
        tier = "action" if commented or supports else ("watch" if n["id"] in nprr_notes else "none")
        nprrs.append({
            "id": n["id"], "url": n["url"], "title": n["title"], "sponsor": n["sponsor"], "urgent": n["urgent"],
            "posted": n["posted"], "next_group": n["next_group"], "next_step": n["next_step"],
            "stage": stage_order.get(n["next_group"], 1), "last_action": n["actions"][0] if n["actions"] else None,
            "description": n["description"], "imm_documents": n["imm_documents"], "imm_opinion": opinion,
            "tier": tier, "note": nprr_notes.get(n["id"], ""),
        })
    nprrs.sort(key=lambda n: (-n["stage"], {"action": 0, "watch": 1, "none": 2}[n["tier"]], n["id"]))

    # ---------- calendar ----------
    calendar = []
    for m in meetings:
        if m["upcoming"]:
            calendar.append({"date": m["date"], "title": "PUCT open meeting (agenda posted)", "kind": "puct",
                             "url": m["agenda_url"], "basis": "PUCT open meetings page"})
    posted = {c["date"] for c in calendar}
    for e in ercot_cal["events"]:
        if not e["key"] or e["date"] < today:
            continue
        if e["name"].startswith("PUCT"):
            if e["date"] not in posted:
                calendar.append({"date": e["date"], "title": "PUCT open meeting", "kind": "puct", "url": e["url"], "basis": "ERCOT meeting calendar"})
        else:
            calendar.append({"date": e["date"], "title": f"ERCOT {e['name']}", "kind": "ercot", "url": e["url"],
                             "detail": e["body"] if e["body"] != e["name"] else "", "basis": "ERCOT meeting calendar"})
    seen = {(c["date"], c["kind"]) for c in calendar}
    for e in extra_dates:
        if e["date"] < today:
            continue
        if e["kind"] == "puct" and (e["date"], "puct") in seen:
            # Same meeting already pulled automatically: keep the more specific reviewed title.
            for c in calendar:
                if c["date"] == e["date"] and c["kind"] == "puct":
                    c["title"], c["basis"] = e["title"], c["basis"] + "; " + e.get("basis", "")
            continue
        calendar.append(e)
    calendar.sort(key=lambda c: (c["date"], c["kind"]))

    # ---------- which meeting the page leads with ----------
    with_agenda = sorted(m["date"] for m in meetings if m["upcoming"])
    future_puct = sorted(c["date"] for c in calendar if c["kind"] == "puct")
    next_date = with_agenda[0] if with_agenda else (future_puct[0] if future_puct else None)
    focus = with_agenda[0] if with_agenda else (meetings[0]["date"] if meetings else None)
    next_meeting = {"date": next_date, "agenda_posted": bool(with_agenda)}

    # ---------- action items generated from the data ----------
    auto = []
    if with_agenda:
        m = next(x for x in meetings if x["date"] == with_agenda[0])
        nums = lambda tier: ", ".join(str(i["num"]) for i in m["items"] if i["tier"] == tier)
        detail = f"Action items: {nums('action') or 'none'}." + (f" Watch items: {nums('watch')}." if nums("watch") else "")
        auto.append({"id": "auto-attend", "auto": True, "title": f"Attend the {datetime.strptime(m['date'], '%Y-%m-%d').strftime('%B %-d')} open meeting",
                     "detail": detail, "due": m["date"], "status": "Open", "source": {"type": "meeting", "date": m["date"]}})
        unsorted = [i for i in m["items"] if i["reason"].startswith("Not matched by any rule")]
        if unsorted:
            auto.append({"id": "auto-unsorted", "auto": True, "title": f"Sort {len(unsorted)} agenda item{'s' if len(unsorted) != 1 else ''} the rules could not place",
                         "detail": "Items: " + ", ".join(str(i["num"]) for i in unsorted) + ".", "due": m["date"], "status": "Open",
                         "source": {"type": "meeting", "date": m["date"]}})
    unread = [x for x in mentions if x["stance"] == "Not yet read"]
    if unread:
        auto.append({"id": "auto-unread", "auto": True, "title": f"Read {len(unread)} new filing{'s' if len(unread) != 1 else ''} that refer{'s' if len(unread) == 1 else ''} to the IMM",
                     "detail": "Found since the last review: " + "; ".join(f"{x['party'][:40]} in {x['control']}" for x in unread[:4]) + ("…" if len(unread) > 4 else "") + ".",
                     "due": "", "status": "Open", "source": None})
    actions = auto + actions

    # ---------- sources and run status ----------
    status = load("status.json") or {}
    sources = []
    for key, name in SOURCE_NAMES.items():
        entry = status.get(key, {})
        sources.append({"key": key, "name": name, "ok": entry.get("ok"), "last_ok": entry.get("last_ok", ""),
                        "last_attempt": entry.get("last_attempt", ""), "detail": entry.get("detail", "")})

    # ---------- what changed since the previous pull ----------
    snapshot = {
        "agendas": [m["date"] for m in meetings],
        "outcomes": [m["date"] for m in parsed if m.get("consent") or m.get("not_taken_up")],
        "filings": {c: max((f["item"] for f in p["filings"]), default=0) for c, p in projects.items()},
        "nprr": {n["id"]: n["next_group"] for n in nprrs},
        "mentions": [f"{m['control']}#{m['item']}" for m in mentions],
        "memos": [f"{m['control']}#{m['item']}" for m in memos],
    }
    before = load("seen.json")
    log = load("update_log.json") or []
    changes = ["First pull"] if before is None else what_changed(before, snapshot)
    entry = {"at": now_stamp(), "changes": changes,
             "sources_ok": sum(1 for x in sources if x["ok"]), "sources": len(sources)}
    if log and not changes and not log[-1]["changes"] and log[-1]["at"][:10] == entry["at"][:10]:
        log[-1] = entry      # a second quiet run on the same day replaces the first
    else:
        log.append(entry)
    log = log[-60:]
    (RAW / "seen.json").write_text(json.dumps(snapshot, indent=1), encoding="utf-8")
    (RAW / "update_log.json").write_text(json.dumps(log, indent=1, ensure_ascii=False), encoding="utf-8")

    data = {
        "generated": now_stamp(), "today": today, "site": site, "sources": sources, "log": log[::-1][:30],
        "meetings": meetings, "archive": archive, "actions": actions, "focus_meeting": focus, "next_meeting": next_meeting,
        "recent_filings": recent, "recent_days": RECENT_DAYS, "watched": watched, "memos": memos,
        "mentions": mentions, "mentions_meta": {k: mentions_raw.get(k) for k in ("since", "documents_scanned")},
        "nprrs": nprrs, "opinions_notice": opinions_raw.get("notice"),
        "recommendations": recs, "calendar": calendar, "legislature": legislature,
        "counts": {"watched": len(projects), "filings_indexed": sum(len(p["filings"]) for p in projects.values()),
                   "meetings_listed": len(meetings_raw["meetings"]), "memo_dockets": memos_raw.get("dockets_checked", 0),
                   "memos_since": memos_raw.get("since", ""), "imm_projects": len(footprint), "imm_since": footprint_raw.get("since", ""),
                   "new_week": sum(1 for f in recent if f["date"] >= week_ago)},
    }

    focus_meeting = next((m for m in meetings if m["date"] == focus), None)
    landing = {
        "generated": data["generated"], "today": today, "site": site, "sources": sources, "log": data["log"][:12],
        "next_meeting": next_meeting, "focus": {"date": focus, "counts": focus_meeting["counts"], "items": len(focus_meeting["items"])} if focus_meeting else None,
        "counts": {**data["counts"], "mentions": len(mentions), "docs_scanned": mentions_raw.get("documents_scanned", 0),
                   "nprrs": len(nprrs), "nprrs_at_puct": sum(1 for n in nprrs if n["next_group"] == "PUCT"),
                   "open_actions": sum(1 for a in actions if a["status"] == "Open"), "recs": len(recs["recommendations"])},
    }

    def encode(obj):
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    def standalone(template, payload):
        """Split a template into head and body so the published page is a proper document."""
        page = template.replace("/*__DATA__*/null", payload)
        cut = page.index("</style>") + len("</style>")
        return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
                '<meta name="robots" content="noindex">\n'
                "<style>body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>\n"
                + page[:cut] + "\n</head>\n<body>\n" + page[cut:] + "\n</body>\n</html>\n")

    DIST.mkdir(exist_ok=True)
    dashboard = (SITE / "template.html").read_text(encoding="utf-8")
    (DIST / "dashboard.html").write_text(standalone(dashboard, encode(data)), encoding="utf-8")
    (DIST / "index.html").write_text(standalone((SITE / "landing.html").read_text(encoding="utf-8"), encode(landing)), encoding="utf-8")
    (DIST / "data.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    (DIST / ".nojekyll").write_text("", encoding="utf-8")
    print(f"built dist/index.html and dist/dashboard.html: {len(meetings)} meetings, {len(recent)} recent filings, "
          f"{len(mentions)} mentions, {len(nprrs)} NPRRs, {len(calendar)} calendar entries")
    print("changes since the previous pull: " + ("; ".join(changes) if changes else "none"))


if __name__ == "__main__":
    main()
