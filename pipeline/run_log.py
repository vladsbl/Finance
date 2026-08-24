#!/usr/bin/env python3
"""Shared knowledge of pipeline/run_daily.py's log file: where it lives,
and how to read the last run back out of it.

Deliberately a SEPARATE module from run_daily.py rather than living there:
importing run_daily executes its logging.basicConfig() at module scope,
which would reconfigure the root logger and attach a second FileHandler to
run_daily.log inside whatever process imported it (the FastAPI app, in
practice). This module has no import-time side effect beyond creating the
log directory, so both run_daily.py and api/routers/pipeline.py can import
it safely.

The parser is the only place that knows run_daily.py's log wording, and it
sits next to the constants that wording is built from -- change a marker in
run_daily.py and the matching regex is one file away, not buried in a
router.
"""

import os
import re
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LOG_DIR = os.path.join(REPO_ROOT, "data", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# run_daily.py's own logger output (INFO level, one line per event).
LOG_FILE = os.path.join(LOG_DIR, "run_daily.log")

# Raw stdout/stderr of an API-launched run, overwritten each time. Only
# useful when the process dies BEFORE run_daily.py's logging is configured
# (an ImportError, a broken interpreter path, ...) -- everything after that
# point is already in LOG_FILE.
API_STDOUT_FILE = os.path.join(LOG_DIR, "run_daily_api_stdout.log")

# run_daily.py logs with datefmt="%H:%M:%S": every line carries a TIME but
# no date, and the file is appended to across days. So a timestamp parsed
# out of a line is only ever a time-of-day; the calendar date of the last
# run has to come from the file's own mtime (see parse_last_run's
# "log_modified_at").
_TIME = r"(\d{2}:\d{2}:\d{2})"

_RE_RUN_START = re.compile(rf"^{_TIME} \[INFO\] DEBUT DU PIPELINE QUOTIDIEN \((\d+) etapes\)")
_RE_STEP_START = re.compile(rf"^{_TIME} \[INFO\] ETAPE : (.+?)\s*$")
# Anchored right after "[INFO] " so the BILAN recap lines, which pad the
# status inside the brackets and indent by two spaces ("  [OK   ] name"),
# never match here and double-count a step.
_RE_STEP_OK = re.compile(rf"^{_TIME} \[INFO\] \[OK\] (.+) termine en ([\d.]+)s\.")
_RE_STEP_FAIL = re.compile(rf"^{_TIME} \[ERROR\] \[ECHEC\] (.+) a echoue apres ([\d.]+)s : (.*)$")
_RE_TOTAL = re.compile(
    rf"^{_TIME} \[INFO\] Total : (\d+)/(\d+) etapes reussies, duree globale ([\d.]+)s"
)


def parse_last_run(log_path=LOG_FILE):
    """Read back the MOST RECENT run recorded in `log_path`.

    Returns None when the file is missing or holds no run marker at all
    (fresh clone, log rotated away) -- never raises for a malformed or
    half-written file: a run still in progress is the normal case for this
    parser's main caller, and its last lines are whatever the pipeline had
    flushed at that instant.

    `completed` distinguishes "the run reached its BILAN line" from "the
    log just stops", which is either a run still going or one whose process
    was killed. Callers that know whether a process is still alive (the API
    holds the Popen handle) should trust that over this flag.
    """
    if not os.path.exists(log_path):
        return None

    with open(log_path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    # Only the last run block matters: the file accumulates every run.
    start_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if _RE_RUN_START.match(lines[i]):
            start_idx = i
            break
    if start_idx is None:
        return None

    block = lines[start_idx:]
    steps_total = int(_RE_RUN_START.match(block[0]).group(2))
    started_time = _RE_RUN_START.match(block[0]).group(1)

    steps = []
    pending = None          # step announced by "ETAPE :" but not yet resolved
    finished_time = None
    duree_secondes = None
    n_ok = n_failed = None

    for line in block[1:]:
        m = _RE_STEP_START.match(line)
        if m:
            pending = m.group(2)
            continue

        m = _RE_STEP_OK.match(line)
        if m:
            steps.append({"name": m.group(2), "status": "ok",
                          "elapsed": float(m.group(3)), "error": None})
            pending = None
            continue

        m = _RE_STEP_FAIL.match(line)
        if m:
            steps.append({"name": m.group(2), "status": "failed",
                          "elapsed": float(m.group(3)), "error": m.group(4)})
            pending = None
            continue

        m = _RE_TOTAL.match(line)
        if m:
            finished_time = m.group(1)
            n_ok, steps_total = int(m.group(2)), int(m.group(3))
            duree_secondes = float(m.group(4))
            n_failed = steps_total - n_ok

    if n_ok is None:
        n_ok = sum(1 for s in steps if s["status"] == "ok")
        n_failed = sum(1 for s in steps if s["status"] == "failed")

    return {
        "log_time_start": started_time,
        "log_time_end": finished_time,
        "completed": finished_time is not None,
        "steps": steps,
        "steps_total": steps_total,
        "steps_done": len(steps),
        "n_ok": n_ok,
        "n_failed": n_failed,
        # The step whose "ETAPE :" line was logged with no [OK]/[ECHEC] yet:
        # what the pipeline is working on right now.
        "current_step": pending,
        "duree_secondes": duree_secondes,
        # The only real DATE available (see the datefmt note above).
        "log_modified_at": datetime.fromtimestamp(os.path.getmtime(log_path)).isoformat(
            timespec="seconds"
        ),
    }
