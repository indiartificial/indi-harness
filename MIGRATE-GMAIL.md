# Replacing the Sheets store with Gmail

The Queue and Responses spreadsheets duplicate data Gmail already holds.
This is a sketch of what it takes to drop them, what genuinely can't move,
and the three places where a naive port breaks something quietly.

Nothing here is implemented yet.

## What moves where

| Today | After |
|---|---|
| Queue: `gmail_msg_id`, `thread_id`, `sender`, `sender_addr`, `subject`, `body`, `received_at` | Gmail. Already there; we were copying it. |
| Queue: `status` | Gmail labels (`Indi/Replied`, `Indi/Skipped`, …) |
| Queue: `note` | **No equivalent.** See below. |
| Responses: `response_text`, `created_at` | Gmail Sent / Drafts |
| Outbound: `recipient`, `subject`, `body` | Gmail Sent |
| Outbound: `rationale` | Drive file — *why* Indi wrote is not in the mailbox |
| Journal | Drive file — never touches a mailbox |

Journal and Outbound move to append-only files on Drive via the helpers
already in `memory_file.py`, not to a smaller spreadsheet. That's what lets
the `spreadsheets` scope go; keeping any Sheet keeps the scope.

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

**3. Gmail matches words, not substrings.** The current search is
`q in subject+body` — `"auth"` finds `"authentication"`. Gmail tokenises,
so it won't. The tool description promises substring matching
(`claude_api.py:102`) and would need rewriting, or the model will write
queries that silently return nothing. Mostly this is an upgrade — Gmail
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

**0. Write tests for the store first.** The current 53 are security
guards and import smoke tests — `grep store tests/` returns nothing. The
thing being rewritten has no coverage, and `INDI_SELF_CODE_TEST_COMMAND`
gates Indi's own self-modification on that suite. Rewriting the store
under a suite that can't see it is the largest risk in this plan.

1. Fix `folded` to a stable marker. Independent of everything else, and
   the migration is unsafe until it's done.
2. Add labels alongside the sheet. Write both, read from the sheet.
   Backfill labels from existing Queue rows in a one-off pass.
3. Build the Gmail-backed readers — search, `replies_in_last_day`,
   `known_correspondents` — behind a config flag, still reading the sheet
   for real decisions. Log where the two disagree.
4. Watch the disagreements for a few days of real mail.
5. Flip the flag. Keep writing the sheet.
6. Move Journal and Outbound to Drive files.
7. Stop writing the sheet. Drop the `spreadsheets` scope and re-auth.

Steps 2–5 exist because the store is Indi's memory. A hard cutover that
gets the label backfill wrong doesn't throw — it just makes Indi forget
people, and the heartbeat will cheerfully journal about it.

## The thing worth arguing about

The Sheets are an audit surface. They're readable without a client, they
sit outside the mailbox Indi acts on, and they record *why* — `note`,
`rationale` — which the mailbox never will.

Indi currently self-modifies with auto-apply and sends without review,
and holds `gmail.modify`, which permits trashing messages. No code path
does that, but the scope allows it. Folding the record into Gmail means
the agent's memory and the agent's workspace become the same mutable
store, with no independent copy.

Keeping the Journal and Outbound rationale on Drive preserves most of
this. Whether that's enough is a judgement call about how much
independent record you want, not a technical question — and it's the
part of this plan I'd want your answer on before writing any code.
