"""Pull Texas Legislature Online: upcoming committee hearings and the status of tracked bills."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CURATED, FetchError, clean, fetch_text, load, now_stamp, record_status, save, strip_scripts

BASE = "https://capitol.texas.gov"
FEEDS = {
    "House": f"{BASE}/MyTLO/RSS/RSS.aspx?Type=upcomingmeetingshouse",
    "Senate": f"{BASE}/MyTLO/RSS/RSS.aspx?Type=upcomingmeetingssenate",
}
# Committees and topics that usually carry ERCOT, PUCT and wholesale market bills.
RELEVANT = re.compile(r"business (?:&|and) commerce|state affairs|energy resources|electric|ERCOT|utilit|grid|power", re.I)


def fetch_hearings():
    hearings, status = [], {}
    for chamber, url in FEEDS.items():
        try:
            feed = fetch_text(url, max_age_hours=6)
        except FetchError as err:
            status[chamber] = f"could not load: {err}"
            continue
        if "<rss" not in feed and "<channel" not in feed:
            status[chamber] = "could not load: the feed did not return a feed"
            continue
        items = re.findall(r"<item>(.*?)</item>", feed, re.S)
        real = []
        for item in items:
            title = clean(re.search(r"<title>(.*?)</title>", item, re.S).group(1)) if "<title>" in item else ""
            link = clean(re.search(r"<link>(.*?)</link>", item, re.S).group(1)) if "<link>" in item else ""
            desc = clean(re.search(r"<description>(.*?)</description>", item, re.S).group(1)) if "<description>" in item else ""
            if re.search(r"no committee meetings scheduled", title + desc, re.I):
                continue
            real.append({"chamber": chamber, "title": title, "detail": desc, "url": link,
                         "relevant": bool(RELEVANT.search(title + " " + desc))})
        status[chamber] = f"{len(real)} hearing(s) posted" if real else "No committee meetings scheduled"
        hearings.extend(real)
    return hearings, status


def bill_status(session, bill):
    url = f"{BASE}/BillLookup/History.aspx?LegSess={session}&Bill={bill}"
    text = clean(strip_scripts(fetch_text(url, max_age_hours=72)))
    def grab(pattern):
        m = re.search(pattern, text)
        return m.group(1).strip() if m else ""
    return {
        "session": session, "bill": bill, "url": url,
        "caption": grab(r"Caption Text:\s*(.*?)\s*Author:"),
        "author": grab(r"Author:\s*(.*?)\s*(?:Coauthor|Sponsor|Cosponsor|Subjects):"),
        "last_action": grab(r"Last Action:\s*(.*?)\s*(?:Council Document|Caption Version)"),
    }


def main():
    hearings, status = fetch_hearings()
    tracked = load("tracked_bills.json", CURATED, {"bills": []})["bills"]
    earlier = {(b.get("session"), b.get("bill")): b for b in (load("legislature.json") or {}).get("bills", []) if "error" not in b}
    bills, failed = [], 0
    for entry in tracked:
        notes = {k: v for k, v in entry.items() if k not in ("session", "bill")}
        try:
            found = bill_status(entry["session"], entry["bill"])
            if not found["caption"]:
                raise FetchError("bill page had no caption")
            bills.append({**found, **notes})
        except FetchError as err:
            failed += 1
            kept = earlier.get((entry["session"], entry["bill"]))
            bills.append({**kept, **notes} if kept else {**entry, "error": str(err)})
    feeds_ok = not any(v.startswith("could not load") for v in status.values())
    save("legislature.json", {"fetched": now_stamp(), "feed_status": status, "hearings": hearings, "bills": bills})
    print("hearing feeds:", status)
    for b in bills:
        print(f"  {b['session']} {b['bill']}: {b.get('last_action', '')[:60]} | {b.get('caption', '')[:90]}")
    ok = feeds_ok and not failed
    record_status("tlo", ok, f"{len(hearings)} hearings posted, {len(bills) - failed} of {len(bills)} bill histories read")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
