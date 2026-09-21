# The attachment store

Attachments used to exist for one run. A file someone sent was
downloaded, shown to the model and dropped; a file Indi sent was
assembled from an `[ATTACHMENT: ...]` block, put in a MIME part and
forgotten. The queue sheet recorded the covering email and nothing else,
so "the spreadsheet you sent me last week" was a question Indi could not
answer about a file it had written itself.

Now every attachment that passes through, in either direction, is
written to disk and can be listed and read back later.

## Where things live

| What | Where |
| --- | --- |
| The files themselves | `data/attachments/<message-id>/<filename>` |
| Metadata about them, one JSON object per line | `data/attachments/index.jsonl` |
| Storing, listing, reading, and the two tools | `attachments.py` |
| Incoming files, stored as they are downloaded | `claude_api.build_attachment_blocks` |
| Outgoing files, stored after the reply has gone | `check_and_reply.process_message` |
| Which files belong to a message, in the sheet | the `attachments` column on the Queue tab |

One directory per Gmail message id, both directions together: "what
files belong to this exchange" is the question that actually gets asked,
and the direction is a property of the file, not of the conversation.

`index.jsonl` records the path, filename, size, direction, timestamp,
correspondent's address, subject and message id. It is bookkeeping, not
the record of truth — the files are. Everything that reads the store
walks the directories first and then enriches what it found from the
index, so a lost or half-written index costs metadata and never costs a
file.

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| `INDI_STORE_ATTACHMENTS` | `true` | Off means nothing is written and the tools are not offered. |
| `INDI_ATTACHMENTS_DIR` | `data/attachments` | Relative paths resolve against the harness directory. |
| `INDI_MAX_STORED_ATTACHMENT_MB` | `10` | Per file. |
| `INDI_MAX_STORED_ATTACHMENTS_PER_MESSAGE_MB` | `25` | Per message directory, counting what is already there. |

Going over either cap is a logged skip, not an error: a 400 MB video
attached to an email is not a reason for the reply to fail.

Note that `INDI_MAX_INCOMING_ATTACHMENT_MB` (5 by default) still governs
whether an attachment is downloaded from Gmail at all. Nothing that is
never downloaded can be stored, so in practice that is the lower of the
two ceilings on the incoming side.

## What is stored, and what isn't

Incoming: everything whose bytes the harness holds, including file types
the model cannot read. A `.docx` is unreadable today and is still the
document the conversation is about, so it is kept rather than discarded.
Attachments Gmail refused to hand over, or that were over
`INDI_MAX_INCOMING_ATTACHMENT_MB`, are never downloaded and so are never
stored; the reply notes them as "not read" as it always did.

Outgoing: every file actually sent, which is both the ones from the
model's `[ATTACHMENT: ...]` blocks and the copy of the Terms the gate
attaches. Stored after the send succeeds, so the store never holds
something that never went out. Dry runs store nothing.

Filenames arrive in MIME headers written by strangers, so they are
sanitised through the same `gmail_io.sanitise_filename` the outgoing
direction already used: reduced to a basename, exotic characters
replaced, leading dots stripped so nothing can land as a hidden file.
`../../../etc/passwd` becomes `passwd`. A collision is de-duplicated —
the second `report.pdf` becomes `report-2.pdf` — never overwritten.

## The tools

`list_stored_attachments` gives each file's path, size, direction, date,
correspondent and subject, with an optional substring filter over
filename, subject and address.

`read_stored_attachment` takes a path of the form
`<message-id>/<filename>` and returns the text of text-like files,
truncated at `INDI_MAX_INLINE_ATTACHMENT_CHARS`. Binary files get a
plain "binary, not readable as text" rather than replacement characters.
Contents come back inside the usual untrusted-content markers: a file
someone emailed in is untrusted for exactly the reason the email was.

Both resolve every path through `local_tools.resolve_within`, which
follows symlinks and rejects anything landing outside the store — the
same function the local file tools use, so there is one containment rule
in this harness rather than two that can drift apart. It brings its
denylist with it, which means an attachment that arrived named
`credentials.json` or `id_rsa` is stored but not readable through the
tool.

**Scope.** The tools are offered to exactly the senders who get
`search_message_history`, and are scoped the same way it is: a
correspondent who may only search their own thread may only list and
read their own files. A file the index cannot attribute to the person
asking is invisible to them — unattributed means hidden, not shared, so
anything stored before the index existed is visible only to a sender
with full history scope. Refusing a file that belongs to someone else
uses the same wording as a file that isn't there, because whose file it
is, is itself information about somebody else's correspondence.

## The sheet column

The Queue tab gained an `attachments` column holding a comma-separated
list of store paths. It is last on purpose: `_ensure_headers` upgrades an
existing sheet by appending whatever columns it lacks, so a column at the
end is the only kind that leaves every existing row's values where they
are, and `set_status` addresses `status`..`note` as one contiguous range
that must stay contiguous. Rows written before this change simply have
nothing in the new column; `_load` pads short rows, so reading one back
gives `""` rather than an error.

Incoming files are recorded on the row as soon as they are down, and the
row is rewritten with the outgoing ones after the reply is sent. If a run
dies in between, the sheet still says where the incoming files went.

## Retention

There is none: nothing deletes from `data/attachments/`, which is what
section 3.1 of the Terms tells correspondents to expect. Pruning it is a
manual job, and deleting a file is safe — the listing walks the
filesystem, so a removed file simply stops appearing. Stale index lines
for files that no longer exist are ignored.

The directory holds other people's files and is gitignored, alongside
`data/correspondents.db`, for the same reason.
