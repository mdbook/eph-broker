#!/usr/bin/env python3
"""inbox_accounts.py: the fail-closed account/email/ledger/posture facts for the consolidated
inbox/triage class.

Account identity, email, and seen-ledger path used to be BAKED per-account into separate signed prompts, one
per account. Now there is ONE signed prompt (prompts/inbox/triage.md) and these facts ride the worker's
runtime -p brief instead. This module is the single source of truth for that mapping, and the fail-closed
gate for an unrecognized account (never fall back to a permissive default; see
modules/constraints/inbox-triage.md constraint 12).

The signed prompt is fully ACCOUNT-AGNOSTIC: it names no account anywhere. Each account's per-account
posture (the content that used to live in modules/scopes/inbox-triage.md, "Per-account posture") lives in
the account table's "posture" field, and inbox_fact_block appends it to the runtime fact text.

Value-free: this only maps known account keys to their fixed facts. It does not decide what mail to
touch in general (the shared bucket-table judgment lives in the signed prompt), but the PER-ACCOUNT
posture decisions (archive-for-real vs would-archive-only, backlog caps, alias handling) live in the table.
"""

import json
import logging
import os
import re

import site_env

# The accounts themselves (addresses, ledgers, postures: personal data) live in the private overlay
# config/inbox-accounts.json (not shipped; the schema is in
# config/inbox-accounts.example.json). If the default file is missing (an OSS install with no inbox),
# ACCOUNTS is empty and every inbox spawn refuses via UnknownInboxAccount (fail closed, logged once when an
# inbox row is on). An explicit BROKER_INBOX_ACCOUNTS that does not exist (typo) or a malformed file RAISES at
# import: a half-read account table must never reach a Gmail-capable worker.
_HERE = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_PATH = os.getenv("BROKER_INBOX_ACCOUNTS") or os.path.join(
    os.getenv("CONFIG_DIR", os.path.join(_HERE, "config")), "inbox-accounts.json")
_FIELDS = ("email", "ledger", "posture")
# an account key becomes an MCP server-name suffix
# (`google-workspace-<account>`, cc._merge_inbox_chat_mcp_config) that the TRASH-guard hook matches with a
# regex built from these SAME keys (cc._INBOX_CHAT_TRASH_MATCHER), a key containing "_" would still be a
# syntactically valid dict key here but would desync the two (the old literal `[^_]+` matcher silently missed
# it). Keys are plain lower-kebab tokens only; this is the single place that could ever admit one that isn't.
_ACCOUNT_KEY_RE = re.compile(r"^[a-z0-9-]+$")
# cc._sweep_stale_inbox_chat_mcp_configs deletes every `inbox-chat-mcp-*.json`
# in the SAME mcp_config_dir an account's own `<account>.json` lives in (cc._merge_inbox_chat_mcp_config).
# An account key that itself starts with this prefix would name a per-account file the sweep treats as its
# OWN stale combined-config leftover and deletes outright, reject that key here, at the single source of
# truth for account keys, so the two can never collide.
_RESERVED_ACCOUNT_PREFIX = "inbox-chat-mcp-"


def load_accounts(path=ACCOUNTS_PATH, explicit=None):
    """-> {account: {email, ledger, posture}}. A missing file gives {} (plus a warning) unless `explicit` (default: was
    BROKER_INBOX_ACCOUNTS set?), then FileNotFoundError. Not an object / unknown top-level key / an account
    key not matching ^[a-z0-9-]+$ / an account key starting with "inbox-chat-mcp-" / an account that is not
    exactly {email, ledger, posture} of non-empty strings -> ValueError (fail closed: a half-read or
    malformed account table must never reach a Gmail-capable worker)."""
    if explicit is None:
        explicit = bool(os.getenv("BROKER_INBOX_ACCOUNTS"))
    data = None if explicit or path != ACCOUNTS_PATH else site_env.section("inbox-accounts")
    if data is not None:
        if os.path.exists(path):
            logging.getLogger("eph-broker.inbox_accounts").warning("broker.yaml sets inbox-accounts, %s is ignored",
                                                                   path)
        path = f"{site_env.BROKER_CONFIG} inbox-accounts"
    elif not os.path.exists(path):
        if explicit:
            raise FileNotFoundError(f"BROKER_INBOX_ACCOUNTS={path} does not exist")
        if os.getenv("INBOX_ROW_SOURCE") or os.getenv("ZULIP_ROW_SOURCE"):   # only worth saying with an inbox row on
            logging.getLogger("eph-broker.inbox_accounts").warning(
                "no %s: no inbox accounts, every inbox spawn refuses (see config/inbox-accounts.example.json)", path)
        return {}
    else:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    if not isinstance(data, dict) or set(data) - {"_comment", "accounts"} or not isinstance(data.get("accounts"), dict):
        raise ValueError(f"{path}: want {{\"accounts\": {{...}}}} (+ optional _comment), nothing else")
    for name, facts in data["accounts"].items():
        if not (name and isinstance(name, str) and _ACCOUNT_KEY_RE.fullmatch(name)):
            raise ValueError(f"{path}: account key {name!r} must match {_ACCOUNT_KEY_RE.pattern!r} "
                             f"(plain lower-kebab; it becomes an MCP server-name suffix)")
        if name.startswith(_RESERVED_ACCOUNT_PREFIX):
            raise ValueError(f"{path}: account key {name!r} must not start with {_RESERVED_ACCOUNT_PREFIX!r} "
                             "(reserved for the inbox-chat combined-config sweep; would collide with it)")
        if not (isinstance(facts, dict) and set(facts) == set(_FIELDS)
                and all(isinstance(facts[k], str) and facts[k].strip() for k in _FIELDS)):
            raise ValueError(f"{path}: account {name!r} must be exactly {{email, ledger, posture}}, non-empty strings")
    return data["accounts"]


ACCOUNTS = load_accounts()


class UnknownInboxAccount(Exception):
    pass


def inbox_fact_block(account):
    """Return the runtime fact text to append to the worker's -p brief for `account`.

    RAISES UnknownInboxAccount for anything not in ACCOUNTS: fail closed, never guess a
    permissive default (the signed prompt's own constraint 12 mirrors this at the model layer).

    The returned text carries the email/ledger facts PLUS this account's full posture block
    (moved out of the signed prompt when it became account-agnostic) appended to the tail,
    clearly delimited.
    """
    if account not in ACCOUNTS:
        raise UnknownInboxAccount(account)
    facts = ACCOUNTS[account]
    return (
        f"Runtime account brief: you are triaging account `{account}` "
        f"(email: {facts['email']}, seen-ledger: {facts['ledger']}).\n\n"
        "## Your account's posture (apply this)\n"
        f"{facts['posture']}"
    )


def inbox_chat_fact_block():
    """the chat worker's runtime fact block, listing EVERY account in ACCOUNTS (not just one,
    the chat worker now has Gmail access to all of them, see cc._merge_inbox_chat_mcp_config). Instructs it to
    search across every account when the operator's message doesn't name one, but to ACT (archive/label/
    draft) only on the ONE account a given message actually lives in, applying THAT account's own posture,
    never another account's. Never raises (unlike inbox_fact_block): this is not a per-account gate, it is
    the full picture handed to an already-admitted worker; the spawn-time fail-closed check on an unknown/
    unmapped bot still runs separately in cc.py before this is ever called. An empty ACCOUNTS (no overlay / OSS
    install) returns a plain no-accounts notice, every inbox spawn already refuses before reaching this."""
    if not ACCOUNTS:
        return "Runtime account brief: no inbox accounts are configured (fail closed)."
    header = (f"Runtime account brief: you may work on ANY of these {len(ACCOUNTS)} accounts. Search across "
              "all of them when the operator's message doesn't name one; ACT (archive/label/draft) ONLY on "
              "the one account the message you're acting on actually lives in, applying THAT account's own "
              "posture below (never another account's). Say which account each result came from.")
    blocks = [header]
    for account in sorted(ACCOUNTS):
        facts = ACCOUNTS[account]
        blocks.append(
            f"## Account `{account}` (email: {facts['email']}, seen-ledger: {facts['ledger']})\n\n"
            f"{facts['posture']}"
        )
    return "\n\n".join(blocks)
