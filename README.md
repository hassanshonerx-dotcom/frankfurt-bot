# frankfurt-bot

## Status dashboard

`dashboard/server.py` serves a small page showing the bot's current state,
the result of its recent appointment checks and the output of the last run,
with a button to start a check on demand:

    python dashboard/server.py        # http://localhost:3000

Starting a check needs Playwright, the same requirement `monitor.py` has.
Checks started from the page run in `MONITOR_RUNTIME_DIR` (default
`/tmp/frankfurt-monitor`) with their own copy of `monitor_state.json`, so
the tracked state file is never rewritten from here. `PORT` sets the
listening port.