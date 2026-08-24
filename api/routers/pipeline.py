"""POST /api/pipeline/run + GET /api/pipeline/status -- trigger and follow
pipeline/run_daily.py from the React UI.

Launched as a SEPARATE PROCESS (subprocess.Popen), not via FastAPI's
BackgroundTasks and not in-process:

  * run_daily.py takes minutes and calls each step's main() in-process, so
    running it inside the API would tie up a threadpool worker for the whole
    duration, and any step that hard-crashes the interpreter (or hangs on a
    socket) would take uvicorn down with it.
  * A child process also keeps the pipeline's own logging.basicConfig()
    away from the API's root logger -- the reason pipeline/run_log.py
    exists as a side-effect-free module for the two to share the log path
    and its parser.

Progress is NOT tracked by streaming the child's output: run_daily.py
already writes every step boundary to data/logs/run_daily.log, so the
status route just parses that file (pipeline/run_log.py's parse_last_run).
That also means a run started by Windows Task Scheduler (pipeline/
run_daily.bat) still shows up in "last run", even though this API process
never launched it.

State for the CURRENTLY-LAUNCHED run is kept in module memory rather than
in the database: it is a property of this process (it owns the Popen
handle), it must not outlive a restart -- a "running" flag persisted in
SQLite would be stuck forever if the API were killed mid-run -- and the
durable part (when the last run happened, which steps passed) already lives
in the log file.
"""

import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException

from pipeline.run_log import API_STDOUT_FILE, LOG_FILE, parse_last_run

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUN_DAILY_SCRIPT = os.path.join(REPO_ROOT, "pipeline", "run_daily.py")

# Guards _run/_process together: /status is polled every few seconds while a
# run is live, and /run must never race with it into launching a second
# pipeline against the same SQLite file.
_lock = threading.Lock()

_IDLE_STATE = {
    "task_id": None,
    "status": "idle",       # idle | running | success | failed
    "started_at": None,
    "finished_at": None,
    "returncode": None,
    "error": None,
}
_run = dict(_IDLE_STATE)
_process = None


def _spawn():
    """Start run_daily.py detached from this request. Sole launch seam, so
    tests can replace it and never actually run the pipeline.

    stdout/stderr go to a FILE, never subprocess.PIPE: nothing here reads
    the child's output, and an unread pipe fills its buffer and deadlocks
    the pipeline partway through. The file only matters when the child dies
    before run_daily.py configures its own logging (bad interpreter,
    ImportError); everything after that point is in LOG_FILE."""
    stdout_file = open(API_STDOUT_FILE, "w", encoding="utf-8")
    try:
        return subprocess.Popen(
            [sys.executable, "-u", RUN_DAILY_SCRIPT],
            cwd=REPO_ROOT,
            stdout=stdout_file,
            stderr=subprocess.STDOUT,
        )
    finally:
        # Popen duplicates the handle for the child, so this process can
        # drop its own copy right away instead of leaking it for minutes.
        stdout_file.close()


def _reconcile_locked():
    """Turn a finished child process into a final status. Called on EVERY
    read/write of the state rather than from a watcher thread: the only
    thing that can change without us acting is the child exiting, and
    poll() answers that in microseconds."""
    global _process
    if _process is None or _run["status"] != "running":
        return
    returncode = _process.poll()
    if returncode is None:
        return

    _run["returncode"] = returncode
    _run["finished_at"] = datetime.now().isoformat(timespec="seconds")
    # run_daily.py exits 1 when ANY step failed, 0 when all nine passed --
    # a non-zero code is "some steps failed", not necessarily a crash, so
    # the message points at the per-step detail rather than claiming the
    # whole run died.
    if returncode == 0:
        _run["status"] = "success"
        _run["error"] = None
    else:
        _run["status"] = "failed"
        _run["error"] = (
            f"Le pipeline s'est termine avec le code {returncode} : au moins une etape a "
            "echoue (detail par etape dans last_run.steps)."
        )
    _process = None


def _public_state_locked():
    return {
        **_run,
        # Durable half of the picture, straight from run_daily.py's own log:
        # survives an API restart and covers Task Scheduler runs too.
        "last_run": parse_last_run(LOG_FILE),
        "log_file": LOG_FILE,
    }


@router.get("/status")
def get_pipeline_status():
    """Current run state plus the last recorded run's per-step detail.

    `status` describes what THIS process launched (idle when it has not
    launched anything since startup); `last_run` is parsed from
    data/logs/run_daily.log and is therefore still populated after a
    restart, or when the pipeline was started by Task Scheduler. While a
    run is live, last_run.current_step / steps_done / steps_total give the
    progress the UI shows."""
    with _lock:
        _reconcile_locked()
        return _public_state_locked()


@router.post("/run", status_code=202)
def run_pipeline():
    """Start run_daily.py in the background and return immediately (202
    Accepted) with the task id -- the pipeline takes minutes, so the caller
    polls /api/pipeline/status instead of waiting on this request.

    409 when a run launched by THIS process is still alive, so a double
    click (or two open tabs) cannot put two pipelines on the same SQLite
    file. Known limitation: a run started outside this process -- Windows
    Task Scheduler via pipeline/run_daily.bat -- is not visible to that
    guard; it would need a cross-process lock owned by run_daily.py
    itself."""
    global _process
    with _lock:
        _reconcile_locked()

        if _run["status"] == "running":
            raise HTTPException(
                status_code=409,
                detail=(
                    "Un recalcul est deja en cours (lance a "
                    f"{_run['started_at']}). Attendez qu'il se termine avant d'en "
                    "relancer un : deux pipelines simultanes ecriraient dans la meme "
                    "base SQLite."
                ),
            )

        if not os.path.exists(RUN_DAILY_SCRIPT):
            raise HTTPException(
                status_code=500,
                detail=f"Script introuvable : {RUN_DAILY_SCRIPT}",
            )

        try:
            _process = _spawn()
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Impossible de lancer le pipeline : {exc}",
            ) from exc

        _run.update(
            task_id=uuid.uuid4().hex[:12],
            status="running",
            started_at=datetime.now().isoformat(timespec="seconds"),
            finished_at=None,
            returncode=None,
            error=None,
        )
        return _public_state_locked()
