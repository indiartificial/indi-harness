# Moving Indi from Windows to Hetzner

Your Python files are byte-identical to the last version, so nothing needed
rewriting. Three things were wrong in the layout and config, all fixed in
this directory.

## What was fixed

**1. The tests weren't protected.** `test_guards.py` and `test_imports.py`
were at the top level. `self_code.py` protects the `tests/` directory, not
loose files — so as laid out, Indi could have rewritten the test suite that
gates its own code changes. Moved into `tests/`, which makes them protected.

**2. The test command couldn't run.** `INDI_SELF_CODE_TEST_COMMAND` is
`python3 -m pytest tests -q`, and there was no `tests` directory: pytest
exited with "file or directory not found: tests". Every self-modification
would have been rejected as a test failure. Same fix.

**3. `INDI_SELF_CODE_SENDERS=sjhalasz@gmail`** — missing `.com`, so it
matched nobody and self-modification was silently off. Corrected in
`.env.server`.

Also changed for the move: workspace paths point at the directories
`setup-hetzner.sh` actually created, local execution is on with the Docker
backend, and `INDI_SEND_AUTOMATICALLY=false` for the first run so you can
confirm the server works before it starts sending. CRLF stripped throughout.

## Steps

### 1. Stop the Windows tasks first

Both machines would otherwise poll the same mailbox and both reply — the run
lock is per-machine and can't see across hosts.

    schtasks /Change /TN "Indi Mail" /DISABLE
    schtasks /Change /TN "Indi Heartbeat" /DISABLE

Confirm nothing is mid-run, then continue.

### 2. Upload the harness

From the folder containing `indi-hetzner`:

    $files = (Get-ChildItem .\indi-hetzner -Force).FullName
    scp -r $files indi@<server-ip>:/home/indi/harness/

### 3. Upload the credentials separately

These are deliberately not in this package.

    scp .\indi\token.json .\indi\credentials.json .\indi\api_key.txt `
        indi@<server-ip>:/home/indi/harness/

`token.json` transfers fine — it's account credentials, not machine-bound.
No need to redo the OAuth flow.

### 4. Set up, on the server

    ssh indi@<server-ip>
    cd ~/harness
    chmod 600 api_key.txt token.json credentials.json
    mv .env.server .env
    mkdir -p ~/workspace ~/workspace-out
    ~/venv/bin/python -m pytest tests -q

53 tests should pass. If the venv isn't there:

    python3 -m venv ~/venv
    ~/venv/bin/pip install -q anthropic google-api-python-client \
        google-auth-oauthlib python-dotenv pytest

### 5. Dry run

    ~/venv/bin/python check_and_reply.py --dry-run
    ~/venv/bin/python check_and_reply.py --heartbeat --dry-run

Writes nothing anywhere. Confirms Google auth, Sheets access, the API key,
and the config all work from the new host.

### 6. Install the timers, as root

    exit
    scp indi-hetzner\deploy\indi*.service indi-hetzner\deploy\indi*.timer `
        root@<server-ip>:/etc/systemd/system/
    ssh root@<server-ip>
    systemctl daemon-reload
    systemctl enable --now indi.timer indi-heartbeat.timer
    systemctl list-timers 'indi*'

### 7. Watch one real cycle, then enable sending

    journalctl -u indi -f

Once a cycle looks right, edit `~/harness/.env`, set
`INDI_SEND_AUTOMATICALLY=true`, and save. No restart needed — each run reads
the config fresh.

## Not starting if it's already running

You get this at two levels, and they cover different failure modes.

**systemd, by unit semantics.** A service unit is either active or it isn't,
so systemd will never run two instances of `indi.service` at once. If the
timer fires while a run is in progress, the job waits rather than starting a
second copy.

**The timer interval.** `indi.timer` now uses:

    OnUnitInactiveSec=5min

rather than `OnUnitActiveSec`. The difference matters. `OnUnitActiveSec`
measures from when the run *started*, so a run that takes seven minutes has
its next trigger already overdue and fires immediately on finishing — a
backlog that never drains. `OnUnitInactiveSec` measures from when the run
*finished*, giving a real five-minute gap between runs whatever their
duration.

**The lock file, as backstop.** `locking.py` takes an exclusive `flock` on
`indi.lock` and exits with "Another run is already in progress" if it can't.
That covers what systemd doesn't: you running the script by hand while a
timed run is going. The OS releases it if the process dies, so a crash never
leaves a stale lock.

To check what actually happened:

    systemctl list-timers 'indi*'          # last run, next run
    journalctl -u indi --since '1 hour ago'
    tail -50 ~/harness/log.txt

## Afterwards

Once a day or two has gone well, remove the Windows scheduled tasks rather
than leaving them disabled:

    schtasks /Delete /TN "Indi Mail" /F
    schtasks /Delete /TN "Indi Heartbeat" /F

Keep the Windows copy of `api_key.txt`, `token.json` and `credentials.json`
somewhere offline as a backup, then delete them from the working folder.
