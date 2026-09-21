# The terms gate

Nobody gets a substantive reply from Indi until they have accepted the
Terms of Service. This is enforced by ordinary code in front of the
reply model, not by an instruction in the system prompt: the model is
never asked whether the gate applies.

## Where things live

| What | Where |
| --- | --- |
| The agreement, one file per version | `data/terms/v0.2.txt` |
| Loading it, and working out which version is current | `terms.py` |
| Who has been sent it, and who accepted | `data/correspondents.db` (SQLite) |
| Reading and writing that database | `correspondents.py` |
| The gate itself, and every fixed message it sends | `terms_gate.py` |
| Where it runs | `check_and_reply.process_message`, before the model |

To publish a new version, drop `data/terms/v0.3.txt` next to the
existing file. Nothing else needs changing: the highest version number
present is the one in force, the version string comes from the filename,
and correspondents who accepted an older version are re-papered
automatically. Do not delete old versions — they are what a recorded
acceptance refers to.

The database path can be moved with `INDI_CORRESPONDENTS_DB`, which
exists mainly so the tests have somewhere to write. It is not in
`config.py` with the other paths on purpose: the gate's own bookkeeping
should not be relocatable or disable-able by a setting.

## What happens to an incoming message

1. **The operator** (`sjhalasz@gmail.com`, the one hardcoded constant in
   `terms_gate.py`) is exempt and gets normal service always.
2. **"I AGREE"** — punctuation, case, whitespace and the quoted chain
   are all ignored, so `i agree.` counts — records the acceptance with
   the time, the address and the version, and sends a short
   confirmation. The next message gets a normal reply.
3. **"DECLINE"** records the refusal, acknowledges it once, and then
   that address hears nothing further, ever, unless it later agrees.
4. **Anything that reads like a life-threatening emergency** gets the
   fixed 911/988 notice and is forwarded to the operator, whatever the
   sender's status. A declined address is still forwarded but still gets
   no reply.
5. **Accepted, current version** — normal service, unchanged.
6. **Accepted, older version** — normal service, with the new terms
   attached and a one-line note that continued use is acceptance, per
   clause 8.2. Never blocked.
7. **Never seen before** — the covering note and the terms, and nothing
   else. The reply model is not called.
8. **Sent the terms, wrote back something that wasn't "I AGREE"** — one
   more copy, and then silence until they do agree.

Everyone who corresponded with Indi before this feature existed counts
as never seen before, so everyone is re-papered on their next message.
There was no record of anyone having accepted anything, and inventing
one would have defeated the point.

## The parts that fail closed

* **No terms on disk.** If `data/terms/` is missing or empty, anyone who
  has not already accepted gets no reply at all and an error goes in the
  log. The harness does not fall back to a built-in copy of the terms;
  an approximation of a contract is not a contract.
* **The emergency classifier fails open in the other direction.** It is
  one cheap yes/no call on Haiku, and if it errors the message is
  treated as ordinary and the failure is logged. A classifier that
  failed the other way would turn an API outage into a stream of crisis
  notices to people discussing their weekend, and a warning that arrives
  for no reason teaches people to ignore the one that matters.
* **The forwarder has no recipient parameter.** `forward_to_operator`
  can address the operator and nobody else. Incoming mail is
  attacker-controlled text; a forwarding destination that its content
  could influence would be an exfiltration channel. There is a test.

## Unsolicited mail

The heartbeat and scheduled reminders go through the same gate. The
heartbeat is only shown correspondents who have accepted, `outbound._send`
checks again before anything leaves, and a reminder that comes due for
someone who has not accepted is held rather than sent — it expires on
its own after the usual grace window if they never do.

## Editing what people read

The covering note, the acceptance confirmation, the decline
acknowledgement and the emergency reply are plain-text constants at the
top of `terms_gate.py`. Each ends with the standard `--- Indi`
signature, and a test enforces that.
