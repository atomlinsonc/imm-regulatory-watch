"""Pull ERCOT sources: pending Nodal Protocol Revision Requests (NPRRs) and the meeting calendar."""
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import FetchError, clean, fetch_text, load, now_stamp, record_status, save

BASE = "https://www.ercot.com"
NPRR_LIST = f"{BASE}/mktrules/issues/nprr"
CALENDAR = f"{BASE}/calendar"
# Stakeholder bodies that handle market rules, in the order a revision request normally travels.
KEY_BODIES = re.compile(r"\b(Board|R&M|Reliability and Markets|TAC|PRS|WMS|ROS|WMWG|CMWG|PUCT)\b")


def labelled(page, label):
    m = re.search(rf"<th>\s*{re.escape(label)}:?\s*</th>\s*<td>(.*?)</td>", page, re.S)
    return clean(m.group(1)) if m else ""


def mdY(text):
    try:
        return datetime.strptime(text.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def nprr_detail(issue):
    url = f"{BASE}/mktrules/issues/{issue}"
    page = fetch_text(url, max_age_hours=6)
    actions = []
    for row in re.findall(r"<tr>\s*<td class=\"date\">(.*?)</tr>", page, re.S):
        cells = [clean(c) for c in re.split(r"</td>", "<td>" + row)][:4]
        if len(cells) == 4 and 'class="nextStep"' in row:
            actions.append({"date": mdY(cells[0]), "body": cells[1], "action": cells[2], "next": cells[3]})
    documents = []
    for href, title, caption in re.findall(
            r'<a download href="([^"]+)" title="([^"]+)".*?<span class="">([^<]*)</span>', page, re.S):
        posted = None
        try:
            posted = datetime.strptime(caption.split(" - ")[0].strip(), "%b %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
        documents.append({"name": clean(title), "url": href, "posted": posted})
    posted = labelled(page, "Date Posted")
    try:
        posted = datetime.strptime(posted, "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        pass
    return {
        "id": issue,
        "url": url,
        "title": labelled(page, "Title"),
        "next_group": labelled(page, "Next Group"),
        "next_step": labelled(page, "Next Step"),
        "status": labelled(page, "Status"),
        "posted": posted,
        "sponsor": labelled(page, "Sponsor"),
        "urgent": labelled(page, "Urgent"),
        "description": labelled(page, "Description"),
        "actions": actions,
        "document_count": len(documents),
        "imm_documents": [d for d in documents if re.search(r"\bIMM\b|Potomac", d["name"])],
        "latest_documents": sorted(documents, key=lambda d: d["posted"] or "", reverse=True)[:4],
    }


def fetch_nprrs():
    page = fetch_text(NPRR_LIST, max_age_hours=6)
    issues = list(dict.fromkeys(re.findall(r'<td><a href="/mktrules/issues/(NPRR\d+)">', page)))
    if not issues:
        raise FetchError("no revision requests found on the list page (the site may be blocking this machine)")
    earlier = {n["id"]: n for n in (load("nprr.json") or {}).get("nprrs", []) if "error" not in n}
    out, failed = [], 0
    for issue in issues:
        try:
            detail = nprr_detail(issue)
            if not detail["title"]:
                raise FetchError("detail page had no title")
            out.append(detail)
        except FetchError as err:
            failed += 1
            out.append(earlier.get(issue) or {"id": issue, "url": f"{BASE}/mktrules/issues/{issue}", "error": str(err)})
    if failed > len(issues) / 2:
        raise FetchError(f"{failed} of {len(issues)} revision request pages could not be read")
    save("nprr.json", {"fetched": now_stamp(), "source": NPRR_LIST, "nprrs": out})
    return out


def fetch_calendar():
    page = fetch_text(CALENDAR, max_age_hours=6)
    events = []
    pattern = re.compile(
        r'<a href="(https://www\.ercot\.com/calendar/(\d{2})(\d{2})(\d{4})-[^"]+)"(?: title="([^"]*)")?>\s*(.*?)\s*</a>'
        r'(.*?)<span class="startTime">([^<]*)</span>', re.S)
    for url, mm, dd, yyyy, full, short, between, start in pattern.findall(page):
        name = clean(short)
        events.append({
            "date": f"{yyyy}-{mm}-{dd}",
            "time": start.strip(),
            "name": name,
            "body": clean(full) or name,
            "location": clean(re.sub(r"<a .*", "", between, flags=re.S)).replace(" & ", " and "),
            "url": url,
            "key": bool(KEY_BODIES.search(name)),
        })
    if not events:
        raise FetchError("no events found on the ERCOT calendar page (the site may be blocking this machine)")
    events.sort(key=lambda e: (e["date"], e["time"]))
    save("ercot_calendar.json", {"fetched": now_stamp(), "source": CALENDAR, "events": events})
    return events


def main():
    failures = 0
    try:
        nprrs = fetch_nprrs()
        ok = [n for n in nprrs if "error" not in n]
        print(f"pending NPRRs: {len(nprrs)} ({len(ok)} read)")
        record_status("ercot_nprr", True, f"{len(ok)} pending revision requests read")
    except Exception as err:
        failures += 1
        record_status("ercot_nprr", False, err)
        print(f"ERCOT revision requests: FAILED ({err})")
    try:
        events = fetch_calendar()
        print(f"ERCOT calendar events: {len(events)} ({sum(e['key'] for e in events)} key bodies), {events[0]['date']} to {events[-1]['date']}")
        record_status("ercot_calendar", True, f"{len(events)} upcoming events")
    except Exception as err:
        failures += 1
        record_status("ercot_calendar", False, err)
        print(f"ERCOT calendar: FAILED ({err})")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
