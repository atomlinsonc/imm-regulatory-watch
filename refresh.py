"""Pull every source again and rebuild the pages. Run from this folder:

    python refresh.py            # everything
    python refresh.py --no-scan  # skip downloading and reading filed documents (the slow step)

A source that cannot be reached does not stop the run: the pages keep its last good data
and show it as not reached. Only a failed build makes this script exit with an error.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STEPS = [
    ("PUCT agendas, filings and commissioner memos", "fetch_puct.py"),
    ("ERCOT rule changes and calendar", "fetch_ercot.py"),
    ("Texas Legislature", "fetch_tlo.py"),
    ("Filed documents: references to the IMM", "scan_documents.py"),
    ("Build the pages", "build.py"),
]

failed = []
for label, script in STEPS:
    if script == "scan_documents.py" and "--no-scan" in sys.argv:
        continue
    print(f"\n== {label}", flush=True)
    # -I keeps Python from importing anything out of the download cache.
    result = subprocess.run([sys.executable, "-I", str(HERE / "pipeline" / script)], cwd=HERE)
    if result.returncode != 0:
        failed.append(label)
        if script == "build.py":
            print("\nThe build failed, so the pages were not updated.")
            sys.exit(1)

if failed:
    print("\nNot fully reached (the pages show the last good data for these): " + "; ".join(failed))
print("\nOpen dist/index.html to view the result.")
