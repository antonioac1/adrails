# The adsops unattended guard

The guard is the part of adsops that runs when no agent and no human is watching.
It is a single stdlib-only command:

```
__PYTHON__ -m adsops --config __CONFIG__ guard-all
```

The units in this directory run that command **every 15 minutes**: launchd
`StartInterval 900` (plus `RunAtLoad`), systemd `OnCalendar=*:0/15` with
`Persistent=true`, cron `*/15 * * * *`.

Nothing in this repository installs, loads, enables or schedules the guard for
you. The files here are templates: you render the placeholders and register them
with your own operating system, after reading them.

## What the guard does

On every run, for every account and campaign in the config, it:

1. takes the **account lock** (one writer per account at a time, so a guard run
   and an agent commit can never interleave),
2. reads live state from the ad platform,
3. evaluates the guards taken from the **policy file** — never from agent input:
   `max_daily_budget_usd`, `max_end_date`, `lifetime_spend_pause_usd`,
   `daily_spend_pause_usd`, `max_ops_per_run`,
4. if a threshold is crossed, issues **pause** operations only,
5. writes the heartbeat file, appends to the audit log, and exits.

The guard is **pause-only**. It never raises a budget, never pushes an end date
out, never enables a paused campaign, and never creates new spend exposure.
Anything that increases exposure has to come from a reviewed plan through the
`plan` / `apply` tools — which is exactly the path that the heartbeat freshness
check below protects.

## Why it must be scheduled, not run from the chat session

A chat-session MCP process cannot watch spend. The adsops MCP server lives only
as long as an agent session does: when the conversation ends, the editor closes,
or the laptop lid shuts, the process is gone. Nothing inside that process can
notice at 03:00 that a day's spend has crossed a threshold.

The operating system's scheduler is the only participant guaranteed to still be
there. So the spend checks are delegated to launchd / systemd / cron, and the MCP
server stays a request/response tool that agents call. This is why "just leave
the MCP server running" is not an answer, and why the guard has its own units.

## The heartbeat file and `max_guard_age_min`

Every successful run writes an ISO-8601 UTC timestamp to the heartbeat file
(`guard_heartbeat` in the config, typically `~/.adsops/guard-heartbeat`). The
file's mtime is the machine-readable signal; the content is for humans and logs.

`max_guard_age_min` in the **policy file** turns that signal into a rail:

- `null` (the default) — the check is off. A stale heartbeat is a warning only.
- an integer, e.g. `20` — every change that would **increase spend exposure**
  (raising a campaign budget, enabling a campaign, extending an end date) is
  **refused** while the heartbeat is missing or older than that many minutes.

That is the fail-closed direction: if the guard is not running, adsops does not
silently lose its protection, it loses the *ability to increase spend*. Pauses,
budget decreases and date shortenings still go through; increases wait until the
guard has run again and the heartbeat is fresh. Set `max_guard_age_min` to a
small multiple of the scheduling interval (15 min → 20–45 min), not to something
so large that a dead guard goes unnoticed.

## What happens when the machine sleeps

Laptops sleep and desktops get suspended. All three schedulers behave the same
way here: **while the machine is asleep, no checks run.**

The consequence chain is worth stating plainly:

1. the machine sleeps → the guard cannot run → no spend checks happen,
2. ad delivery does not stop because your laptop did → overspend is possible
   during that window,
3. on wake, the heartbeat is older than the sleep duration → it goes **stale**,
4. stale heartbeat + `max_guard_age_min` set → **exposure-increasing changes are
   blocked** until a guard run refreshes it (often immediately, since launchd's
   `RunAtLoad` and systemd's `Persistent=true` catch up on wake).

So a sleeping machine does not silently disable your rails, but it does mean the
threshold may be crossed while nothing is running. If that window is unacceptable,
run the guard on a machine that stays awake — a small always-on host or a
container/service on a server — not on a laptop that closes its lid.

## What this is not

This guard is a threshold with automatic pause attempts, not a hard spend ceiling.
Google Ads can serve up to 2× the average daily budget on any given day (and can
spend the full daily budget within hours), and platform reporting lags by minutes
to hours. Pauses are API calls that can fail, be rejected, or land late. Treat the
numbers as an alerting-and-containment mechanism that bounds damage, not as a
billing guarantee. Keep account-level payment limits and platform-side billing
caps as the real ceiling.

## Install steps

First render the four placeholders in whichever template you use:
`__PYTHON__` (absolute interpreter path), `__CONFIG__` (absolute config path),
`__PKG__` (directory containing the `adsops` package), `__LOGDIR__` (writable
log directory). Verify a run by hand before scheduling it, and check the log
afterwards:

```
PYTHONPATH=__PKG__ __PYTHON__ -m adsops --config __CONFIG__ guard-all
ls -l __LOGDIR__/adsops-guard.log
```

### macOS (launchd)

```
mkdir -p __LOGDIR__
sed -e "s|__PYTHON__|$PY|g" -e "s|__CONFIG__|$CFG|g" \
    -e "s|__PKG__|$PKG|g" -e "s|__LOGDIR__|$LOGDIR|g" \
    deploy/launchd/com.adsops.guard.plist.template \
    > ~/Library/LaunchAgents/com.adsops.guard.plist
plutil -lint ~/Library/LaunchAgents/com.adsops.guard.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.adsops.guard.plist
launchctl list | grep com.adsops.guard
```

Runs every 900 s and once at load. To stop or remove it:
`launchctl bootout gui/$(id -u)/com.adsops.guard`.

### Linux (systemd)

```
sudo install -d -m 0755 __LOGDIR__
sudo sed -e "s|__PYTHON__|$PY|g" -e "s|__CONFIG__|$CFG|g" \
             -e "s|__PKG__|$PKG|g" -e "s|__LOGDIR__|$LOGDIR|g" \
             deploy/systemd/adsops-guard.service \
             > /etc/systemd/system/adsops-guard.service
sudo cp deploy/systemd/adsops-guard.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now adsops-guard.timer
systemctl list-timers adsops-guard.timer
journalctl -u adsops-guard.service -n 50
```

The service is `Type=oneshot`; the timer fires it every quarter hour and catches
up on missed runs after downtime.

### Any POSIX host (cron)

```
mkdir -p __LOGDIR__
sed -e "s|__PYTHON__|$PY|g" -e "s|__CONFIG__|$CFG|g" \
    -e "s|__PKG__|$PKG|g" -e "s|__LOGDIR__|$LOGDIR|g" \
    deploy/cron/adsops-guard.cron > /tmp/adsops-guard.cron
crontab -l > /tmp/adsops-cron.backup 2>/dev/null || true
cat /tmp/adsops-guard.cron >> /tmp/adsops-cron.backup
crontab /tmp/adsops-cron.backup
crontab -l
```

cron does not inherit your shell environment, hence the explicit
`PYTHONPATH=__PKG__` line in the template.

## Verifying the guard is alive

- `ls -l ` on the heartbeat path — the mtime should be under 15–20 minutes old.
- Tail `__LOGDIR__/adsops-guard.log` — each run logs what it checked and paused.
- `deploy/smoke_docker.sh` runs the full selftest in a throwaway container; if it
  passes, the package itself is healthy and any problem is scheduling, paths or
  credentials.
