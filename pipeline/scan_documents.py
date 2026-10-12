"""Download recent filings in the watched projects, extract their text, and record
every place another party refers to the IMM ("who is talking about us")."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from datetime import date, timedelta

from common import CACHE, FetchError, TooLarge, fetch, load, now_stamp, record_status, save
from fetch_puct import document_files

# Rolling window: filings made in the last 90 days.
SINCE = (date.today() - timedelta(days=90)).isoformat()
TEXT_DIR = CACHE / "text"
MAX_BYTES = 12_000_000
# "IMM" must be capitals: scanned exhibits are full of stray "Imm" fragments.
MENTION = re.compile(r"(?-i:\bIMM(?:'s|’s)?\b)|Independent Market Monitor|Potomac Economics|market monitor", re.I)
IMM_PARTY = re.compile(r"potomac", re.I)
# Individual public comments and confidential filings are skipped.
SKIP_TYPES = {"PC", "CONF"}
SKIP_DESCRIPTION = re.compile(r"project raptor|audit request|request for audit|^void|voided", re.I)


def filing_text(control, filing):
    """Extracted text of a filing's PDFs as a list of (page_number, text). Cached on disk."""
    from pypdf import PdfReader
    out_path = TEXT_DIR / f"{control}_{filing['item']}.txt"
    if out_path.exists():
        pages = out_path.read_text(encoding="utf-8").split("\f")
        return [(i + 1, t) for i, t in enumerate(pages)]
    pages = []
    for url in document_files(filing["url"]):
        if not url.lower().endswith(".pdf"):
            continue
        try:
            path = fetch(url, max_age_hours=None, suffix=".pdf", max_bytes=MAX_BYTES)
        except TooLarge:
            continue
        try:
            for page in PdfReader(str(path)).pages:
                pages.append(page.extract_text() or "")
        except Exception:
            continue
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\f".join(pages), encoding="utf-8")
    return [(i + 1, t) for i, t in enumerate(pages)]


def snippets(pages, limit=6):
    found = []
    for number, text in pages:
        flat = re.sub(r"\s+", " ", text)
        for m in MENTION.finditer(flat):
            start = max(flat.rfind(". ", 0, m.start()) + 2, m.start() - 260)
            stop = flat.find(". ", m.end())
            stop = min(stop + 1 if stop != -1 else len(flat), m.end() + 320)
            passage = flat[start:stop].strip()
            if not any(passage[:80] == f["text"][:80] for f in found):
                found.append({"page": number, "text": passage})
    return found[:limit], len(found)


def main():
    projects = load("filings.json")["projects"]
    results, scanned, eligible = [], 0, 0
    for control, project in sorted(projects.items()):
        for filing in project["filings"]:
            if not filing["date"] or filing["date"] < SINCE:
                continue
            if filing["type"].strip() in SKIP_TYPES or SKIP_DESCRIPTION.search(filing["description"]):
                continue
            eligible += 1
            try:
                pages = filing_text(control, filing)
            except FetchError as err:
                print(f"  skip {control} #{filing['item']}: {err}")
                continue
            scanned += 1
            if IMM_PARTY.search(filing["party"]):
                continue  # the IMM's own filings are not "others talking about us"
            found, total = snippets(pages)
            if total:
                results.append({**filing, "control": control, "style": project["style"],
                                "mention_count": total, "pages": len(pages), "snippets": found})
                print(f"  {control} #{filing['item']} {filing['date']} {filing['party'][:28]:<28} {total} mention(s)")
    print(f"scanned {scanned} of {eligible} filings since {SINCE}; {len(results)} refer to the IMM")
    if eligible and scanned < eligible / 2:
        # Most documents could not be downloaded: keep the earlier results instead of replacing them with a partial set.
        record_status("documents", False, f"only {scanned} of {eligible} filings could be read")
        sys.exit(1)
    save("mentions.json", {"fetched": now_stamp(), "since": SINCE, "documents_scanned": scanned, "mentions": results})
    try:
        opinions = board_opinions()
        if opinions:
            print(f"IMM opinions found in ERCOT's {opinions['notice']['date']} notice: {len(opinions['opinions'])}")
    except FetchError as err:
        print(f"Board report opinions: skipped ({err})")
    record_status("documents", True, f"{scanned} filings read, {len(results)} refer to the IMM")



# ---------- IMM opinions recorded in ERCOT Board reports ----------

REQUEST_ID = re.compile(r"\b(NPRR|NOGRR|PGRR|LPGRR|RMGRR|RRGRR|SMOGRR|VCMRR|COPMGRR|OBDRR|SCR)\s*-?\s*(\d{2,4})\b")


def board_opinions():
    """Each Board report ERCOT files with the PUCT states the IMM's opinion on that revision request.
    Read the most recent notice in Project 54445 and pull those statements out."""
    project = load("filings.json")["projects"].get("54445", {"filings": []})
    notices = [f for f in project["filings"]
               if re.search(r"NOTICE OF RECOMMENDED APPROVAL", f["description"], re.I) and f["date"]]
    if not notices:
        return None
    notice = max(notices, key=lambda f: f["date"])
    flat = re.sub(r"\s+", " ", " ".join(text for _, text in filing_text("54445", notice)))
    opinions = {}
    for m in re.finditer(r"Independent Market Monitor Opinion\s+(.{0,400}?)(?=\s+ERCOT Opinion|\s+\d{2,4}[A-Z]+-\d+ Board Report|\s+Board Report)", flat):
        statement = m.group(1).strip()
        sentence = re.search(r"IMM [^.]*\.(?:\s*\([^)]*\))?", statement)
        statement = sentence.group(0) if sentence else statement
        found = REQUEST_ID.search(statement)
        if not found:
            continue
        request = found.group(1) + found.group(2)
        if re.search(r"\bno opinion\b", statement, re.I):
            position = "No opinion"
        elif re.search(r"\bsupports?\b", statement, re.I):
            position = "Supports"
        elif re.search(r"\b(opposes?|does not support|concern)", statement, re.I):
            position = "Opposes or has concerns"
        else:
            position = "See statement"
        opinions.setdefault(request, {"request": request, "position": position, "statement": statement})
    data = {"fetched": now_stamp(), "notice": notice, "opinions": list(opinions.values())}
    if opinions:
        save("board_opinions.json", data)
    return data


if __name__ == "__main__":
    main()
