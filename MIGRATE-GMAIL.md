# Replacing the Sheets store with Gmail

The Queue and Responses spreadsheets duplicate data Gmail already holds.
This is a sketch of what it takes to drop them, what genuinely can't move,
and the three places where a naive port breaks something quietly.

Steps 0, 1, 2 and 3 of the order of work below are done; the cutover
itself has not started. Every reader still reads the sheet — the Gmail
readers of step 3 only run in its shadow and log disagreements.

## What moves where

| Today | After |
|---|---|
| Queue: `gmail_msg_id`, `thread_id`, `sender`, `sender_addr`, `subject`, `body`, `received_at` | Gmail. Already there; we were copying it. |
| Queue: `status` | Gmail labels (`Indi/Replied`, `Indi/Skipped`, …) |
| Queue: `note` | Journal file on Drive, keyed by `gmail_msg_id` (settled; see below) |
| Responses: `response_text`, `created_at` | Gmail Sent / Drafts |
| Outbound: `recipient`, `subject`, `body` | Gmail Sent |
| Outbound: `rationale` | Drive file — *why* Indi wrote is not in the mailbox |
| Journal | Drive file — never touches a mailbox |

Journal and Outbound move to append-only files on Drive via the helpers
already in `memory_file.py`, not to a smaller spreadsheet. That's what lets
the `spreadsheets` scope go; keeping any Sheet keeps the scope. *Settled:*
they stay on Drive and do **not** fold into Gmail, deliberately — see
"The independent record" at the end.

Final scopes: `gmail.modify` + `drive.file`.

## The state machine

`status` has five values (`sheets_queue.py:55`) and `TERMINAL_STATUSES`
decides retry eligibility. Labels carry this fine — they're arbitrary
strings and `gmail.modify` can create them.

    queued -> Indi/Queued      replied -> Indi/Replied
    skipped -> Indi/Skipped    failed  -> Indi/Failed
    ignored -> Indi/Ignored

Read/unread is already the primary dedupe: the poll filters on `UNREAD`
(`check_and_reply.py:205`) and handled messages get marked read. The label
is the second belt that currently catches a message whose mark-read failed.

`note` is the real loss. It holds the failure text and the reason string
("per-sender daily cap reached (3)"), and Gmail has nowhere to put it.
Three options, in order of preference:

1. Keep it in the Journal file, keyed by `gmail_msg_id`. Preserves it,
   costs one more Drive write on the failure path only.
2. Encode coarse reasons as labels (`Indi/Skipped/RateLimit`,
   `Indi/Skipped/Automated`). Loses the free text.
3. Drop it and rely on `log.txt`. Cheapest, and the log already records
   every one of these lines — but the log rotates and the label doesn't.

*Settled: option 1.* `note` goes to the Journal file, keyed by
`gmail_msg_id`. That move happens with the Journal's own at step 6; until
then `note` stays in the Queue sheet, where it is now.

## Three things that break quietly

**1. Scope enforcement must stay in Python.** Today `restrict_to_sender`
is a hard post-filter over rows (`sheets_queue.py:386`) — an untrusted
sender's search *cannot* see another correspondent's mail, regardless of
what the model puts in `query`. If that becomes a `from:` term
concatenated into a Gmail query string, a crafted `query` containing its
own operators could widen the scope back out. Gmail's `q` is a query
language and the model controls part of the string.

So: send the query to Gmail, then **re-filter the returned messages by
sender in Python** before handing anything back. The Gmail-side `from:`
is an optimisation; the Python filter is the control. This is the one
place in the migration where getting it wrong is a security regression
rather than a bug.

**2. `folded` is a positional offset.** `memory_file.py` stores
`folded: N` and slices `rows[self.folded:]` (`memory_file.py:194`), where
`rows` is `messages_for_sender()` — filtered to `status == replied`,
sorted by `received_at`. It is an index into a list, not a marker.

Change what produces that list and every existing memory file silently
misaligns: memory either re-folds messages it already folded or skips
messages it never did. Neither raises anything.

Fix this *before* the store changes, as its own step: replace the count
with the `received_at` of the last folded message, or a set of folded
`gmail_msg_id`s. Then the offset can't drift when the source changes.

*Done (step 1).* The header is now v2 and names the folded messages:

    # indi-memory v2 | sender: alice@example.com | folded_ids: 18c1…,18c2… | updated: 2026-09-24T10:49:14+00:00

`folded_ids` is a comma-separated, sorted set of Gmail message ids (empty
for none). An id set rather than a timestamp because it is exact: no tie
rule, and a retried message whose `received_at` is older than the last
fold is still pending instead of silently skipped. v1 files (`folded: N`)
are still read; N is resolved against the current `messages_for_sender()`
ordering into the ids of `rows[:N]` — exactly what the old slice treated
as folded — and the next consolidation writes v2. A headerless file is
folded-nothing, as before. Every deployed file has to go through one
consolidation *on the Sheets store* before step 5 flips the reader, or
its v1 count will be resolved against a different ordering.

**3. Gmail matches words, not substrings.** The current search is
`q in subject+body` — `"auth"` finds `"authentication"`. Gmail tokenises,
so it won't. The tool description promises substring matching
(`history_search_schema` in `claude_api.py`) and would need rewriting, or
the model will write queries that silently return nothing. *Deferred to
step 5:* there is a `TODO(MIGRATE-GMAIL.md, step 5)` beside the
description. It must keep describing the Sheets search while the sheet
answers, and must change in the same step that flips the reader. Mostly this is an upgrade — Gmail
brings `from:`, `newer_than:`, `subject:`, negation, none of which exist
today — but it is not a superset of current behaviour.

## Smaller ports

**`replies_in_last_day`** → `in:sent to:X newer_than:2d`, then filter by
`internalDate` in Python. Not `newer_than:1d` directly: Gmail's relative
dates are coarse, and the current check is an exact rolling 24h cutoff
(`sheets_queue.py:356`). Over-fetch and narrow precisely, or the rate
limit drifts by up to a day.

**`known_correspondents`** → everyone in Sent, plus senders carrying an
Indi label. Note this *widens*: today it's only `replied`/`ignored`
senders and prior outbound recipients. Sent contains everything the
account ever mailed, including pre-Indi history. It gates who Indi may
write to on its own initiative, so filter it deliberately rather than
taking Sent wholesale.

**Search result bodies** cost one `messages.get` per hit — 5 API calls at
the current limit, against one cached sheet read today. Fine at this
volume; worth knowing before raising `INDI_MAX_HISTORY_SEARCH_RESULTS`.

**Body truncation goes away.** A Sheets cell caps at 50k chars, so bodies
are truncated on write today (`sheets_queue.py:285`). Gmail has no such
limit.

## Order of work

**0. Write tests for the store first.** *Done:* `tests/test_store.py`
pins the store's contract — status machine, `messages_for_sender`,
`responses_by_message`, the exact 24h cutoff, `known_correspondents`, and
that a sender-scoped search can't return anyone else's mail whatever the
query holds. Backend details live in its fake and `_store` helper, which
is the part to replace for Gmail. Original note: the current 53 are security
guards and import smoke tests — `grep store tests/` returns nothing. The
thing being rewritten has no coverage, and `INDI_SELF_CODE_TEST_COMMAND`
gates Indi's own self-modification on that suite. Rewriting the store
under a suite that can't see it is the largest risk in this plan.

1. Fix `folded` to a stable marker. Independent of everything else, and
   the migration is unsafe until it's done. *Done:* v2 header with
   `folded_ids`; see "`folded` is a positional offset" above and
   `tests/test_memory_watermark.py`.
2. Add labels alongside the sheet. Write both, read from the sheet.
   Backfill labels from existing Queue rows in a one-off pass.
   *Done:* `SheetStore.set_status` and `append_queue_row` set the
   matching `Indi/*` label and remove the other four, after the sheet
   write succeeds (`STATUS_LABELS` in `sheets_queue.py`,
   `gmail_io.StatusLabeler`). Best-effort: any Gmail failure is logged
   and dropped, and the store's behaviour is otherwise unchanged. Off
   with `INDI_GMAIL_STATUS_LABELS=false`. Nothing reads the labels yet.
   The backfill is `backfill_labels.py`, run by hand — dry run by
   default, `--apply` to label; it skips messages already correct or no
   longer in the mailbox, so it can be re-run. Tests:
   `tests/test_status_labels.py`. **Run the backfill with `--apply` once
   before step 3**, or the Gmail readers will see unlabelled history.
3. Build the Gmail-backed readers — search, `replies_in_last_day`,
   `known_correspondents` — behind a config flag, still reading the sheet
   for real decisions. Log where the two disagree.
   *Done:* `gmail_reads.py`. With `INDI_GMAIL_SHADOW_READS=true` (default
   false) `SheetStore.search`, `replies_in_last_day` and
   `known_correspondents` compute and return their sheet answer exactly as
   before, then hand it to a `ShadowReader` that asks Gmail and logs any
   difference at WARNING — the inputs, both answers as ids or counts, and
   bounded set differences, never bodies. Every Gmail exception is caught
   and logged there. What each reader does:
   - search: `from:X {label:indi-…}` plus the model's query, sent to
     Gmail; every returned message is then re-checked in Python for an
     Indi/* label and, when scoped, for a From holding exactly one
     address whose addr-spec (case-folded) is the correspondent's. Ids
     only, `format=metadata`, at most 25 gets.
   - `replies_in_last_day`: `in:sent to:X newer_than:2d`, cut at the
     exact rolling 24h against `internalDate` in Python, inclusive.
   - `known_correspondents`: senders of messages labelled `Indi/Replied`
     or `Indi/Ignored` (the statuses the sheet counts — not Skipped,
     Failed or Queued), plus To/Cc of mail sent at or after
     `INDI_START_DATE`. Without a start date Sent is left out entirely.
   Call counts are bounded and documented at the top of the module. Tests:
   `tests/test_gmail_reads.py`, against a fake Gmail that deliberately
   ignores `q` and returns the whole mailbox, so the scope tests pass only
   because the Python filter holds.

   Not yet done, and it matters for step 4: **`backfill_labels.py --apply`
   has not been run.** Until it is, historic messages carry no Indi/*
   label, so the Gmail search and `known_correspondents` will under-report
   against the sheet. Those disagreements are the backfill's, not the
   readers'. Other differences to expect rather than chase:
   - search: Gmail matches words, not substrings (see above), and the
     sheet holds bodies truncated at `INDI_MAX_BODY_CHARS`.
   - `replies_in_last_day`: Gmail counts everything *sent* to X —
     heartbeat mail included — while the sheet counts reply rows. With
     `INDI_SEND_AUTOMATICALLY=false` replies are drafts, not sent, so
     Gmail will say 0 where the sheet counts them. Decide which is the
     right rate limit before step 5.
   - `known_correspondents`: the start date, once set, adds anyone mailed
     from the account since then, which the sheet never saw.

   Until config.py grows the settings, the flag and `INDI_START_DATE` are
   read by `gmail_reads.py` through `config._env`, as outbound.py does for
   `INDI_HEARTBEAT_MODEL`.
4. Watch the disagreements for a few days of real mail.
5. Flip the flag. Keep writing the sheet.
6. Move Journal and Outbound to Drive files.
7. Stop writing the sheet. Drop the `spreadsheets` scope and re-auth.

Steps 2–5 exist because the store is Indi's memory. A hard cutover that
gets the label backfill wrong doesn't throw — it just makes Indi forget
people, and the heartbeat will cheerfully journal about it.

## The independent record

The Sheets are an audit surface. They're readable without a client, they
sit outside the mailbox Indi acts on, and they record *why* — `note`,
`rationale` — which the mailbox never will.

Indi currently self-modifies with auto-apply and sends without review,
and holds `gmail.modify`, which permits trashing messages. No code path
does that, but the scope allows it. Folding the record into Gmail would
make the agent's memory and the agent's workspace the same mutable
store, with no independent copy.

*Settled (project owner):* the Journal and the outbound rationale stay
as Drive files, not in Gmail, deliberately, to keep an independent audit
surface outside the mailbox Indi acts on. `note` joins them in the
Journal (option 1 above). The mailbox holds what was said; Drive holds
why.
