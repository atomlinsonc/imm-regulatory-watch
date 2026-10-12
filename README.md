# IMM Regulatory Watch

**Live site: https://atomlinsonc.github.io/imm-regulatory-watch/**

A demonstration dashboard that follows Public Utility Commission of Texas (PUCT) proceedings
from the point of view of ERCOT's Independent Market Monitor (IMM). It is built from public
sources only and is not a Potomac Economics, PUCT or ERCOT work product.

## What it does

Every morning a scheduled GitHub Actions job:

1. **Pulls** the PUCT open meetings page and agenda PDFs, the PUCT Interchange filing index,
   ERCOT's revision request pages and meeting calendar, and Texas Legislature Online.
2. **Reads** new filings in the watched projects, extracts their text, and searches for
   references to the IMM. It also reads the IMM's stated opinion on each rule change out of
   ERCOT's Board reports.
3. **Sorts** agenda items into Action, Watch or Not relevant, each with a reason.
4. **Publishes** the rebuilt pages to GitHub Pages and commits the pulled data to `data/raw/`,
   so each day's changes are in the commit history.

If a source cannot be reached, the pages keep its last good data and show it as not reached.

## Run it yourself

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python refresh.py
```

Then open `dist/index.html`. Add `--no-scan` to skip downloading filed documents (the slow step).

## Layout

| Path | What it is |
|---|---|
| `pipeline/fetch_puct.py` | Open meeting agendas, filing index, IMM party search, commissioner memo search |
| `pipeline/fetch_ercot.py` | Pending Nodal Protocol Revision Requests and the ERCOT meeting calendar |
| `pipeline/fetch_tlo.py` | Committee hearing feeds and bill histories |
| `pipeline/scan_documents.py` | Full-text search of filed PDFs for references to the IMM |
| `pipeline/build.py` | Combines everything and writes the pages to `dist/` |
| `data/raw/` | What the last pull returned, committed daily |
| `data/curated/` | Hand-written judgment: relevance rules, reviewed notes, action items, recommendation status |
| `site/` | Page templates |
| `.github/workflows/daily.yml` | The daily schedule |

## What is judgment

Everything in `data/curated/` was written by hand from the public record. New items that
appear on a later pull are sorted by the keyword rules in `relevance_rules.json` and shown as
auto-sorted until someone adds a reviewed entry. Calendar entries with `"placeholder": true`
are invented to show how IMM deliverables would appear.

## Privacy

PUCT records name members of the public who file comments or complaints, sometimes with a home
address. This project does not store or publish those names: such filings are saved as
"Member of the public", and complaint captions are rewritten without the complainant's name.

## Limits

- Open meetings have video but no transcript, so what was said in the room is not captured.
- Action items come only from the public record.
- Only NPRRs are tracked on the ERCOT side, and only the five most recent agendas are sorted.
