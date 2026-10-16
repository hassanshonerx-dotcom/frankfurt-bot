#!/usr/bin/env python3
"""Status dashboard for the Frankfurt appointment bot.

Serves a small page showing the monitor's current state and its recent
appointment checks, and lets you run a check on demand.

monitor.py itself is untouched. A check started from here runs in a
runtime directory with its own copy of monitor_state.json, so the state
file committed by the GitHub Actions run is never rewritten.
"""

import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
REPO_STATE = ROOT / "monitor_state.json"
RUNTIME_DIR = Path(
    os.environ.get("MONITOR_RUNTIME_DIR", "/tmp/frankfurt-monitor")
)
RUNTIME_STATE = RUNTIME_DIR / "monitor_state.json"
RUN_LOG = RUNTIME_DIR / "runs.jsonl"
INDEX = Path(__file__).resolve().parent / "index.html"

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "3000"))
CHECK_TIMEOUT = 600
MAX_OUTPUT = 4000
KEEP_RUNS = 20

SCHEDULE = "Every 10 minutes (GitHub Actions)"

_run_lock = threading.Lock()
_running = False


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_state(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _current_state():
    """State written by checks run here, otherwise the tracked state."""
    state = _read_state(RUNTIME_STATE)
    if state is not None:
        return state, "checks run here"
    state = _read_state(REPO_STATE)
    if state is not None:
        return state, "tracked monitor_state.json"
    return {}, "no state file"


def _read_runs():
    runs = []
    try:
        lines = RUN_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return runs

    for line in lines[-KEEP_RUNS:]:
        if line.strip():
            try:
                runs.append(json.loads(line))
            except ValueError:
                continue
    return runs


def _record_run(run):
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with RUN_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(run) + "\n")


def _seed_state():
    """Give the first check the bot's committed state to continue from."""
    if not RUNTIME_STATE.exists() and REPO_STATE.exists():
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        RUNTIME_STATE.write_text(
            REPO_STATE.read_text(encoding="utf-8"),
            encoding="utf-8",
        )


def _result_of(run):
    """The verdict monitor.py printed on its last line, if any."""
    for line in reversed(run.get("output", "").splitlines()):
        for token in ("NO_SLOTS", "CHECK_PAGE"):
            if token in line:
                return token
        if "CHECK FAILED" in line:
            return "FAILED"
    return None


def _last_line(text):
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


def run_check():
    """Run monitor.py once. False when a check is already running."""
    global _running

    with _run_lock:
        if _running:
            return False
        _running = True

    try:
        _seed_state()
        started = _now()
        try:
            finished = subprocess.run(
                [sys.executable, str(ROOT / "monitor.py")],
                cwd=RUNTIME_DIR,
                capture_output=True,
                text=True,
                timeout=CHECK_TIMEOUT,
            )
            output = (finished.stdout + finished.stderr).strip()
            exit_code = finished.returncode
        except subprocess.TimeoutExpired:
            output = "Check timed out after %ds." % CHECK_TIMEOUT
            exit_code = -1
        except OSError as error:
            output = "Could not start monitor.py: %s" % error
            exit_code = -1

        _record_run(
            {
                "started": started,
                "finished": _now(),
                "exit_code": exit_code,
                "output": output[-MAX_OUTPUT:],
            }
        )
        print("%s check finished (exit %s)" % (started, exit_code))
    finally:
        with _run_lock:
            _running = False

    return True


def build_status():
    state, state_source = _current_state()
    runs = _read_runs()
    last = runs[-1] if runs else None
    failures = state.get("failures", 0)

    if _running:
        phase = "running"
        headline = "Checking the appointment page..."
        detail = "monitor.py is loading the calendar right now."
    elif last is None:
        phase = "unknown"
        headline = "No checks recorded yet"
        detail = (
            "Run a check to confirm the bot can read the "
            "appointment page."
        )
    elif last.get("exit_code") != 0:
        if _result_of(last) in ("NO_SLOTS", "CHECK_PAGE"):
            phase = "warning"
            headline = "Read the calendar, but the run did not finish cleanly"
        else:
            phase = "error"
            headline = "Last check failed"
        detail = _last_line(last.get("output", "")) or "The check did not finish."
    elif _result_of(last) == "CHECK_PAGE":
        phase = "alert"
        headline = "Calendar loaded, no-availability message missing"
        detail = (
            "An appointment may have opened, or the page changed. "
            "Open the website and check manually."
        )
    else:
        phase = "ok"
        headline = "Checking successfully - no appointments available"
        detail = (
            "The last check loaded the calendar and found the usual "
            "no-availability message."
        )

    last_result = _result_of(last) if last else None

    return {
        "phase": phase,
        "headline": headline,
        "detail": detail,
        "running": _running,
        "facts": {
            "last_check": last.get("started") if last else None,
            "last_result": last_result,
            "failures": failures,
            "schedule": SCHEDULE,
        },
        "output": last.get("output", "") if last else "",
        "state": state,
        "state_source": state_source,
        "checked_url": "https://cal.mofa.gov.iq/12220",
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "frankfurt-dashboard"

    def _send(self, status, body, content_type):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except BrokenPipeError:
            pass

    def _send_json(self, status, data):
        self._send(status, json.dumps(data), "application/json")

    def _route(self):
        return urlsplit(self.path).path

    def do_GET(self):
        path = self._route()
        if path in ("/", "/index.html"):
            try:
                page = INDEX.read_text(encoding="utf-8")
            except OSError:
                self._send(500, "dashboard page is missing", "text/plain")
                return
            self._send(200, page, "text/html; charset=utf-8")
        elif path == "/api/status":
            self._send_json(200, build_status())
        elif path == "/favicon.ico":
            self._send(204, "", "image/x-icon")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self._route() == "/api/check":
            if run_check():
                self._send_json(202, {"started": True})
            else:
                self._send_json(409, {"started": False})
        else:
            self._send(404, "not found", "text/plain")

    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args))


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("dashboard on http://%s:%d" % (HOST, PORT))
    server.serve_forever()


if __name__ == "__main__":
    main()
