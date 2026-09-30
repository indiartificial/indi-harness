"""
self_service.py

Things a correspondent can do for themselves by asking Indi in an email,
rather than through the operator:

  * see the sharing permissions they have given (grants over their own
    mail — audience.py), and withdraw any or all of them;
  * join or leave the daily newsletter.

Every tool here acts on the sender's own address and nothing else: the
address is never an argument. They are offered only when the reply goes
to the sender alone, since a list of whom someone has let see their mail
is theirs, not the thread's.

Withdrawing and leaving only ever narrow what happens, so neither needs
the sender to pass authentication — a person must always be able to stop
sharing. Joining the newsletter is opt-in by this request and nothing
else: the sender asks, for themselves.

This module changes who may see what, so it is protected from
self-modification.
"""

from __future__ import annotations

import audience
import newsletter

LIST_PERMISSIONS_TOOL = "list_my_sharing_permissions"
WITHDRAW_PERMISSION_TOOL = "withdraw_sharing_permission"
SUBSCRIBE_TOOL = "subscribe_me_to_newsletter"
UNSUBSCRIBE_TOOL = "unsubscribe_me_from_newsletter"
TOOL_NAMES = {LIST_PERMISSIONS_TOOL, WITHDRAW_PERMISSION_TOOL, SUBSCRIBE_TOOL,
              UNSUBSCRIBE_TOOL}


def schemas(sender: str) -> list[dict]:
    return [
        {
            "name": LIST_PERMISSIONS_TOOL,
            "description": (
                f"List the sharing permissions {sender} has given: which of "
                "their messages Indi may show to which other people, active "
                "and withdrawn. Use when they ask what they have agreed to share."
            ),
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": WITHDRAW_PERMISSION_TOOL,
            "description": (
                f"Withdraw sharing permissions {sender} has given. Give a "
                "permission id from the list, or a person's address to withdraw "
                "everything shared with them, or \"all\". Use only when they ask "
                "to stop sharing. It stops future use; it cannot recall what "
                "was already sent — tell them so."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "which": {"type": "string",
                              "description": "A permission id, an address, or \"all\"."},
                },
                "required": ["which"],
            },
        },
        {
            "name": SUBSCRIBE_TOOL,
            "description": (
                f"Add {sender} to Indi's daily newsletter. Only when they ask "
                "to receive it themselves; never on their behalf for anyone else."
            ),
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": UNSUBSCRIBE_TOOL,
            "description": f"Remove {sender} from Indi's daily newsletter, when they ask.",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]


def _describe_scope(store, grant) -> str:
    if grant.scope == audience.ALL_ITEMS:
        return "all of your mail"
    try:
        rows = store._rows([grant.scope]) if hasattr(store, "_rows") else []
    except Exception:
        rows = []
    if rows:
        r = rows[0]
        return f"your message of {r.get('received_at', '')[:10]} \"{r.get('subject', '')}\""
    return f"one of your messages ({grant.scope})"


def _list(store, sender: str) -> str:
    mine = [g for g in audience.GrantBook(store).all() if g.owner == sender]
    if not mine:
        return f"{sender} has not given any sharing permissions."
    lines = []
    for g in mine:
        state = f"withdrawn {g.revoked_at[:10]}" if g.revoked_at else "active"
        lines.append(f"{g.grant_id}: {_describe_scope(store, g)} may be shown to "
                     f"{g.to} (given {g.granted_at[:10]}; {state})")
    return "\n".join(lines)


def _withdraw(store, sender: str, which: str) -> tuple[str, bool]:
    which = (which or "").strip().lower()
    if not which:
        return "Say which permission: an id, an address, or \"all\".", True
    book = audience.GrantBook(store)
    active = [g for g in book.all() if g.owner == sender and g.active]
    if which == "all":
        targets = active
    elif "@" in which:
        targets = [g for g in active if g.to == which]
    else:
        targets = [g for g in active if g.grant_id.lower() == which]
    if not targets:
        return f"No active permission of {sender}'s matches {which!r}.", True
    done = [g for g in targets if book.revoke(g.grant_id)]
    if hasattr(store, "_grants"):
        store._grants = None
    return (f"Withdrew {len(done)} permission(s): "
            + "; ".join(f"{g.grant_id} ({g.to})" for g in done)
            + ". This stops future sharing; what was already sent can't be recalled."), False


def handle(cfg, store, sender: str, name: str, tool_input: dict) -> tuple[str, bool]:
    """(text, is_error). `sender` is the authenticated-or-not sender of the
    message being answered — always the subject, never an argument."""
    sender = (sender or "").strip().lower()
    if not sender or store is None:
        return "Not available for this message.", True
    try:
        if name == LIST_PERMISSIONS_TOOL:
            return _list(store, sender), False
        if name == WITHDRAW_PERMISSION_TOOL:
            return _withdraw(store, sender, (tool_input or {}).get("which", ""))
        if name == SUBSCRIBE_TOOL:
            result = newsletter.add_subscriber(store, sender, cfg, added_by="self, by email")
            return result, result.startswith("refused")
        if name == UNSUBSCRIBE_TOOL:
            result = newsletter.remove_subscriber(store, sender, cfg)
            return result, result.startswith("refused")
    except Exception as exc:
        return f"That didn't work: {exc.__class__.__name__}.", True
    return f"Unknown tool {name}.", True
