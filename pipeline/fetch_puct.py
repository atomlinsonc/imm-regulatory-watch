"""Pull PUCT sources: open meeting agendas, the filing index for watched projects,
and the list of projects where the IMM (Potomac Economics) has filed."""
import re
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CURATED, FetchError, clean, fetch, fetch_text, load, now_stamp, record_status, save, table_rows, us_date

OPEN_MEETINGS = "https://www.puc.texas.gov/agency/calendar/openmeetings/"
INTERCHANGE = "https://interchange.puc.texas.gov"
# How many of the most recent open meetings to parse in full.
MEETINGS_TO_PARSE = 5
# Projects watched regardless of what the party search returns.
ALWAYS_WATCH = {
    "55999": "Reports of the Electric Reliability Council of Texas",
    "54445": "Review of Protocols Adopted by the Independent Organization",
    "34677": "Reports of the Independent Market Monitor for the ERCOT Region",
    "59212": "CY 2026 Rulemaking Calendar",
}
IMM_PARTY = "Potomac Economics"
IMM_SINCE = "2025-01-01"

SUBSECTIONS = ["WATER", "ELECTRIC", "COMMUNICATIONS", "GENERAL", "CLOSED SESSION"]

# ---------- keeping private individuals out of the saved data ----------
# The PUCT's records name members of the public who file comments or complaints, sometimes with a
# home address. This project has no use for that, so those names are never stored or published.
ORG_WORDS = re.compile(
    r"\b(LLC|L\.L\.C|LP|L\.P|Inc|Corp|Corporation|Company|Co|Coalition|Association|Associates|Alliance|Council|Cooperative|"
    r"Cooperatives|Authority|City|Club|Fund|Group|Consulting|Network|University|Texas|ERCOT|PUC|Commission|Committee|"
    r"Services?|Partners|Holdings|Systems|Solutions|Infrastructure|Digital|Bitcoin|Blockchain|Capital|Advisors|Markets?|"
    r"Utilit(?:y|ies)|Transmission|Generation|Storage|Solar|Wind|Resources|Development|Foundation|Institute|Office|"
    r"Counsel|Mills|Chemical|Energy|Power|Electric|Policy|Economics|Senator|Representative|Director|Chairman|Commissioner|"
    r"Staff|District|County|Water|Supply|Retail|Municipal|Communities|Clean|Grid|Data|Center|Coalition)\b", re.I)
PERSONAL = re.compile(r"\bmy name is\b|\bI live\b|request(?:ing)? (?:a )?public hearing|in favor of|audit request|request for audit|project raptor|^stop the\b", re.I)


def looks_like_person(name):
    name = name.strip()
    if not name or ORG_WORDS.search(name) or name.isupper():
        return False
    # Drop trailing credentials and stray date fragments before counting name words.
    name = re.sub(r",\s*(MD|PhD|Jr|Sr|PE|Esq)\.?$", "", name)
    name = re.sub(r"\b\d+\b|\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\b", " ", name)
    if re.search(r"\d", name):
        return False
    words = re.findall(r"[A-Za-z][A-Za-z.'’-]*", name)
    return 2 <= len(words) <= 4


def is_public_comment(party, kind, description):
    return kind.strip() == "PC" or bool(PERSONAL.search(description)) or looks_like_person(party)


def redact_title(title):
    """Replace a private complainant's name in a docket caption with a neutral phrase."""
    m = re.match(r"Complaint of (.+?) Against (.+)$", title, re.I)
    if m:
        kept = [part.strip() for part in re.split(r"\s+and\s+", m.group(2)) if ORG_WORDS.search(part)]
        against = " and ".join(kept) if kept else "another party"
        return f"Complaint of a customer against {against}"
    m = re.match(r"(.+?)[’']s Appeal of (.+)$", title)
    if m and not ORG_WORDS.search(m.group(1)):
        return f"A customer’s appeal of {m.group(2)[0].lower()}{m.group(2)[1:]}"
    return title


# ---------- open meetings ----------

def list_meetings():
    """Read the open meetings page: one entry per meeting with its agenda PDF and video link."""
    page = fetch_text(OPEN_MEETINGS, max_age_hours=6)
    links = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page, re.S | re.I)
    meetings, pending_agenda = [], None
    for href, text in links:
        label = clean(text)
        if label == "Meeting Agenda":
            pending_agenda = href
        elif label == "View Meeting" and pending_agenda:
            m = re.search(r"/(\d{4})(\d{2})(\d{2})/?$", href)
            if m:
                meetings.append({
                    "date": "-".join(m.groups()),
                    "agenda_url": pending_agenda,
                    "video_url": href.replace("http://", "https://"),
                })
            pending_agenda = None
    return sorted(meetings, key=lambda m: m["date"], reverse=True)


def pdf_text(path):
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def number_list(text):
    return [int(n) for n in re.findall(r"\d+", text)]


def parse_agenda(text):
    """Turn agenda text into numbered items with their section, control number and requested action."""
    # Remove the running page header, then repair spacing the PDF extraction introduces.
    text = re.sub(r"Page \d+\s+MEETING OF THE P\.U\.C\. AGENDA\s+[A-Za-z]+ \d{1,2}, \d{4}", "\n", text)
    text = re.sub(r"-[ \t]*\n[ \t]*(?=[a-z])", "-", text)          # "power-\nto-choose"
    text = re.sub(r"(\w) -(?=\w)", r"\1-", text)                   # "473 -26-07756", "In -ERCOT"
    text = re.sub(r"((?:Docket|Project) No\.?\s*)(\d) (\d{4})", r"\1\2\3", text)  # "5 9029"
    head = text.split("PUBLIC COMMENT")[0]
    consent = re.search(r"taken up without discussion:(.*?)(?:Items that will not|$)", head, re.S)
    skipped = re.search(r"will not be taken up:(.*?)$", head, re.S)

    # Locate item starts by walking the numbering in order, so stray "12." strings cannot match.
    starts, pos, n = [], 0, 1
    while True:
        m = re.compile(rf"(?m)^\s*{n}\.\s+").search(text, pos)
        if not m:
            break
        starts.append((n, m.start(), m.end()))
        pos, n = m.end(), n + 1

    items, section, subsection = [], "", ""
    for i, (num, start, body_start) in enumerate(starts):
        # Headings that sit between the previous item and this one.
        gap_start = starts[i - 1][2] if i else 0
        for heading in re.finditer(r"(?m)^\s*(?:([IVX]+)\.\s+([A-Z][A-Z ,]+)|([A-Z][A-Z ]{3,}))\s*$", text[gap_start:start]):
            if heading.group(2):
                section, subsection = heading.group(2).strip().title(), ""
            elif heading.group(3).strip() in SUBSECTIONS:
                subsection = heading.group(3).strip().title()
        end = starts[i + 1][1] if i + 1 < len(starts) else len(text)
        body = text[body_start:end]
        # Drop any heading lines that trail the item, then un-wrap it.
        body = re.sub(r"(?m)^\s*(?:[IVX]+\.\s+[A-Z][A-Z ,]+|" + "|".join(SUBSECTIONS) + r")\s*$", "", body)
        if num == starts[-1][0]:
            body = body.split("Persons planning to attend")[0]
        body = re.sub(r"\s+", " ", body).strip()

        if body.startswith("Adjournment for closed session"):
            body = "Adjournment for closed session"
        # Only a control number that opens the item is the item's own; later ones are cross-references.
        control = re.match(r"(Docket|Project) No\.?\s*(\d{4,6})", body)
        marked = body.replace("(s)", "\u27e8s\u27e9")              # keep "Motion(s)" from ending the match
        action = [a.replace("\u27e8s\u27e9", "(s)") for a in
                  re.findall(r"\(([^()]*(?:Order|action|Rehearing|Publication|Adoption)[^()]*)\)", marked)]
        title = body
        if control:
            title = re.sub(r"^(?:Docket|Project) No\.?\s*\d{4,6}(?:\s*;\s*SOAH Docket No\.\s*[\w.\-]+)?\s*[-–—]?\s*", "", body, count=1)
        staff = ""
        if action:
            tail = title.rsplit("(" + action[-1] + ")", 1)
            title = tail[0].strip()
            staff = tail[1].strip(" .") if len(tail) > 1 else ""
        items.append({
            "num": num,
            "section": section,
            "subsection": subsection,
            "kind": control.group(1) if control else "",
            "control": control.group(2) if control else "",
            "title": redact_title(title.strip(" .")),
            "requested_action": action[-1].strip() if action else "",
            "staff": staff,
        })
    return {
        "consent": number_list(consent.group(1)) if consent else [],
        "not_taken_up": number_list(skipped.group(1)) if skipped else [],
        "items": items,
    }


def fetch_meetings():
    meetings = list_meetings()
    if not meetings:
        raise FetchError("no meetings found on the open meetings page")
    earlier = {m["date"]: m for m in (load("meetings.json") or {}).get("meetings", []) if m.get("items")}
    for meeting in meetings[:MEETINGS_TO_PARSE]:
        try:
            path = fetch(meeting["agenda_url"], max_age_hours=6, suffix=".pdf")
            parsed = parse_agenda(pdf_text(path))
            if not parsed["items"]:
                raise ValueError("no agenda items could be read")
            meeting.update(parsed)
        except Exception as err:  # one bad PDF should not stop the run, or wipe a good earlier read
            meeting["error"] = str(err)[:200]
            kept = earlier.get(meeting["date"])
            meeting.update({k: kept[k] for k in ("consent", "not_taken_up", "items")} if kept else {"items": []})
    save("meetings.json", {"fetched": now_stamp(), "source": OPEN_MEETINGS, "meetings": meetings})
    return meetings


# ---------- Interchange filing index ----------

def imm_footprint():
    """Projects where the IMM has filed since IMM_SINCE, from the Interchange party search."""
    y, m, d = IMM_SINCE.split("-")
    url = (f"{INTERCHANGE}/search/dockets/?UtilityType=A&ItemMatch=Equal&FilingParty={quote(IMM_PARTY)}"
           f"&DateFiledFrom={quote(f'{m}/{d}/{y} 00:00:00')}&SortOrder=Ascending")
    projects = []
    for row in table_rows(fetch_text(url, max_age_hours=6)):
        if len(row) >= 4 and row[0][0].isdigit():
            projects.append({
                "control": row[0][0],
                "imm_filings": int(row[1][0]) if row[1][0].isdigit() else None,
                "owner": row[2][0],
                "style": row[3][0],
            })
    if not projects:
        raise FetchError("party search returned no projects")
    save("imm_footprint.json", {"fetched": now_stamp(), "party": IMM_PARTY, "since": IMM_SINCE,
                                "source": url, "projects": projects})
    return projects


def fetch_filings(control):
    url = f"{INTERCHANGE}/search/filings/?ControlNumber={control}"
    filings = []
    for row in table_rows(fetch_text(url, max_age_hours=6)):
        if len(row) >= 5 and row[0][0].isdigit():
            party, kind, description = row[2][0], row[3][0], row[4][0]
            if is_public_comment(party, kind, description):
                party, kind, description = "Member of the public", "PC", "Public comment"
            filings.append({
                "item": int(row[0][0]),
                "date": us_date(row[1][0]),
                "party": party,
                "type": kind,
                "description": description,
                "url": f"{INTERCHANGE}/search/documents/?controlNumber={control}&itemNumber={row[0][0]}",
            })
    return {"control": control, "source": url, "filings": filings}


def document_files(filing_url):
    """The downloadable files attached to one filing."""
    page = fetch_text(filing_url, max_age_hours=None)
    return re.findall(r'href="(https://interchange\.puc\.texas\.gov/Documents/[^"]+)"', page)


def fetch_watched(extra_controls=()):
    watched = dict(ALWAYS_WATCH)
    try:
        footprint = imm_footprint()
    except FetchError:
        footprint = (load("imm_footprint.json") or {}).get("projects", [])
    for project in footprint:
        watched[project["control"]] = project["style"]
    for control, style in extra_controls:
        watched.setdefault(control, style)
    earlier = (load("filings.json") or {}).get("projects", {})
    out, failed = {}, []
    for control, style in sorted(watched.items()):
        try:
            data = fetch_filings(control)
            if not data["filings"] and earlier.get(control, {}).get("filings"):
                raise FetchError("filing list came back empty")
        except FetchError as err:
            # Keep the last good list for this project rather than replacing it with nothing.
            data = {**earlier.get(control, {"control": control, "filings": []}), "error": str(err)[:200]}
            failed.append(control)
        data["style"] = style
        out[control] = data
        print(f"  {control}: {len(data['filings'])} filings  {style[:60]}")
    if len(failed) > len(out) / 2:
        raise FetchError(f"{len(failed)} of {len(out)} project filing lists could not be read")
    save("filings.json", {"fetched": now_stamp(), "projects": out})
    return out


def commissioner_memos(days=45, limit=45):
    """Memos filed by the Chairman or a Commissioner in any docket in the last `days` days.
    The docket search matches on filing description; each hit's filing list is then read for the memo itself."""
    from datetime import date, timedelta
    since = date.today() - timedelta(days=days)
    stamp = quote(since.strftime("%m/%d/%Y") + " 00:00:00")
    dockets = {}
    for term in ("CHAIRMAN", "COMMISSIONER"):
        url = (f"{INTERCHANGE}/search/dockets/?UtilityType=A&ItemMatch=Equal&DateFiledFrom={stamp}"
               f"&SortOrder=Ascending&FilingDescription={quote(term)}")
        for row in table_rows(fetch_text(url)):
            if len(row) >= 4 and row[0][0].isdigit():
                dockets[row[0][0]] = row[3][0]
    memos = []
    for control, style in sorted(dockets.items())[:limit]:
        try:
            filings = fetch_filings(control)["filings"]
        except FetchError:
            continue
        for f in filings:
            if f["date"] and f["date"] >= since.isoformat() and re.search(r"\b(CHAIRMAN|COMMISSIONER)\b.*\bMEMO", f["description"], re.I):
                memos.append({**f, "control": control, "style": style})
    memos.sort(key=lambda f: f["date"], reverse=True)
    save("commissioner_memos.json", {"fetched": now_stamp(), "since": since.isoformat(),
                                     "dockets_checked": len(dockets), "memos": memos})
    return memos


def main():
    failures = 0
    try:
        meetings = fetch_meetings()
        parsed = [m for m in meetings if m.get("items")]
        print(f"open meetings listed: {len(meetings)}; parsed: {len(parsed)}")
        for m in parsed:
            print(f"  {m['date']}: {len(m['items'])} items, consent {len(m['consent'])}, not taken up {len(m['not_taken_up'])}")
        record_status("puct_meetings", True, f"{len(meetings)} meetings listed, {len(parsed)} agendas read")
    except Exception as err:
        failures += 1
        record_status("puct_meetings", False, err)
        print(f"open meetings: FAILED ({err})")
        parsed = [m for m in (load("meetings.json") or {}).get("meetings", []) if m.get("items")]

    # Also watch any agenda item the curated layer marks as relevant or watch, so the brief can cite its filings.
    reviewed = load("agenda_judgments.json", CURATED, {}).get("by_control", {})
    extra = {(i["control"], i["title"]) for m in parsed for i in m["items"]
             if reviewed.get(i["control"], {}).get("tier") in ("action", "watch")}
    try:
        projects = fetch_watched(sorted(extra))
        stale = [c for c, p in projects.items() if p.get("error")]
        record_status("puct_filings", True, f"{len(projects)} projects read" + (f"; {len(stale)} kept from an earlier pull" if stale else ""))
    except Exception as err:
        failures += 1
        record_status("puct_filings", False, err)
        print(f"filing index: FAILED ({err})")

    try:
        memos = commissioner_memos()
        print(f"commissioner memos in the last 45 days: {len(memos)}")
        for memo in memos:
            print(f"  {memo['date']} {memo['control']} #{memo['item']} {memo['description'][:50]} | {memo['style'][:60]}")
        record_status("puct_memos", True, f"{len(memos)} memos found")
    except Exception as err:
        failures += 1
        record_status("puct_memos", False, err)
        print(f"commissioner memos: FAILED ({err})")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
