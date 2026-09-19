# Moving Indi to a Hetzner VPS

Once Indi runs on its own machine, most of what was previously withheld
becomes reasonable. Not because the guards get weaker, but because the
thing behind them changes: a box with no personal documents, no OAuth
token for your Google account, and no filesystem of yours is a box where
"a stranger's code runs here" costs a rebuild rather than your life.

## 1. Create the server

Hetzner Cloud console → Add Server.

| Setting | Choice | Why |
|---|---|---|
| Location | Ashburn or Hillsboro | US, if most correspondents are |
| Image | Ubuntu 24.04 | what `setup-hetzner.sh` expects |
| Type | **CPX11** (2 vCPU, 2 GB, ~€4.5/mo) | Docker sandbox wants headroom; CX22 also fine |
| Volume | none | the harness is tiny; Sheets and Drive hold the data |
| Firewall | skip — `ufw` is configured on the box | one place to reason about |
| SSH key | **yes, add yours** | never use password auth |
| Backups | worth the 20% | one click to roll back a bad self-modification |

Note the IPv4 address. Nothing needs to be reachable from outside except
your SSH, so no DNS, no ports, no reverse proxy.

## 2. Provision

    ssh root@<server-ip> 'bash -s' < deploy/setup-hetzner.sh

Installs Python, git, Docker, `ufw` (deny all inbound but SSH),
`fail2ban`, unattended security upgrades; creates the `indi` user with its
own home; pre-pulls the sandbox image.

To install the systemd units too, copy the directory up first and run it
from inside `deploy/`.

### Logging in as `indi`

`setup-hetzner.sh` copies root's authorized keys to the `indi` user, so
`ssh indi@<server-ip>` works once provisioning finishes. If you set the
server up before that was added, do it once by hand as root:

    install -d -m 700 -o indi -g indi /home/indi/.ssh
    install -o indi -g indi -m 600 /root/.ssh/authorized_keys /home/indi/.ssh/authorized_keys

Either way, you can always reach the account from root with `sudo -iu indi`.
The account has no password and no sudo — that's intentional.

## 3. Move the harness

    rsync -av --exclude '.git' --exclude '__pycache__' \
          ./indi/ indi@<server-ip>:/home/indi/harness/

## 4. Credentials

**Do the OAuth flow on your own machine, not the server.** It opens a
browser, and the script deliberately refuses to start that flow
non-interactively rather than hanging forever waiting for one.

    python check_and_reply.py --dry-run     # locally, once; creates token.json
    scp token.json credentials.json indi@<server-ip>:/home/indi/harness/

Then on the server:

    echo 'sk-ant-...' > ~/harness/api_key.txt
    chmod 600 ~/harness/api_key.txt ~/harness/token.json ~/harness/credentials.json

The token is tied to indiartificial@gmail.com. Since that's Indi's own
account and not yours, a compromise of this box exposes Indi's mailbox,
not yours — which is the point of the separation.

## 5. Configure

    cp .env.example .env && nano .env

What the move unlocks, as a starting point:

    INDI_MODEL=claude-opus-5
    INDI_MAX_TOKENS=16000

    # Anyone may write in, and gets the full off-host toolset.
    INDI_TRUSTED_SENDERS=*
    INDI_HISTORY_ALL_SENDERS=*
    INDI_REQUIRE_AUTHENTICATED_SENDER=true

    # Local files: Indi's own workspace, readable and writable by anyone
    # who writes in. There is nothing of yours on this machine, and the
    # workspace is not the harness directory.
    INDI_ENABLE_LOCAL_FILES=true
    INDI_LOCAL_FILES_ROOT=/home/indi/workspace
    INDI_LOCAL_FILES_WRITABLE_ROOT=/home/indi/workspace-out
    INDI_LOCAL_TOOLS_SENDERS=sjhalasz@gmail.com

    # Docker is real isolation, unlike the subprocess backend.
    INDI_ENABLE_LOCAL_EXEC=true
    INDI_LOCAL_EXEC_BACKEND=docker

    # Initiative.
    INDI_ENABLE_OUTBOUND=true
    INDI_OUTBOUND_MAX_PER_DAY=5
    INDI_OUTBOUND_ALLOW_NEW_RECIPIENTS=false

    # Self-modification.
    INDI_ENABLE_SELF_CODE=true
    INDI_SELF_CODE_SENDERS=sjhalasz@gmail.com
    INDI_SELF_CODE_AUTO_APPLY=true

    # Turn this on last, after a week of reading drafts.
    INDI_SEND_AUTOMATICALLY=false

### Why `INDI_LOCAL_TOOLS_SENDERS` still isn't `*`

Even here, it holds. Anyone with code execution on this box can use it to
reach *other people*: relay spam, scan and attack other hosts from
Hetzner's address space, host content someone else pays for. Those costs
land on strangers who never opted in, and on the account Hetzner bills.
Isolation protects what's on the machine; it doesn't protect the internet
from the machine.

What a wildcard would buy is also smaller than it looks. Strangers already
get code execution — in Anthropic's sandbox, via the `code_execution`
tool, on every message. The gap is only "runs on Indi's own box," and the
honest version of that is a workspace they can use through Indi's
judgement, which is what the configuration above gives them.

If you do want to widen it, widen it by name. The list takes as many
addresses as you care to add.

## 6. Verify, then start

    ~/venv/bin/python ~/harness/check_and_reply.py --dry-run
    ~/venv/bin/python ~/harness/check_and_reply.py --heartbeat --dry-run
    ~/venv/bin/python -m pytest ~/harness/tests -q

Then, as root — `indi` is deliberately not in the sudo group, since it only
needs to run Python in its own home and reach the Docker socket:

    ssh root@<server-ip> 'systemctl enable --now indi.timer indi-heartbeat.timer'
    ssh root@<server-ip> "systemctl list-timers 'indi*'"

Mail check every 5 minutes, heartbeat hourly. The daily budgets in `.env`
are what actually bound Indi's output; the timer only sets how often it
gets the chance to consider acting.

## 7. Watching it

    journalctl -u indi -f              # the reply pipeline
    journalctl -u indi-heartbeat -f    # initiative
    tail -f ~/harness/log.txt          # both, with per-call detail
    cd ~/harness && git log --oneline  # what Indi has changed about itself

Worth reading the first week: every `LOCAL TOOL` line (who asked for what),
every `SELF-CODE` line, and the Journal tab in the Responses spreadsheet —
that last one is the closest thing to seeing what Indi does when nobody is
asking it anything.

## Rolling back a self-modification

    cd ~/harness
    git log --oneline
    git revert <commit>          # or: git reset --hard <good-commit>

Changes take effect on the next timer fire, so a revert is live within
five minutes. Hetzner's snapshots are the backstop if the repo itself gets
into a state you don't like.

## Leaving the Windows machine behind

Once the timers are running, disable whatever Task Scheduler entry you had
locally. Two harnesses polling the same mailbox will both try to reply —
the run lock is per-machine and can't see across hosts.
