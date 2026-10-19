#!/usr/bin/env python3
"""ticket_fetch.py: fetch a Vikunja ticket's rendered content for injection into a worker's brief.

The spawned read-report worker is charter-free and tool-minimal (no Vikunja MCP; the worker stays
pure-compute and credential-free) and would otherwise receive ONLY the coalescer's collapsed brief
(`{n_events, event_names, task_id, last_doer}`), never the ticket's actual title/description. It could
not read its own dispatch ticket, so it reported "cannot read ticket" every cycle.

Fix: the BROKER (not the worker) fetches the ticket via `mcp.sh vikunja-bridge` (same
pattern as poster.py) and injects the rendered text into the brief at spawn (see adapters/cc.py
`CCAdapter._run_worker` / the `ticket_fetcher` kwarg). The worker never touches Vikunja itself.

VALUE-FREE: no secrets/credentials here; mcp.sh owns auth. Structured as a pure argv/json builder
(unit-testable without executing anything) plus a thin exec wrapper that shells out and NEVER raises (a
fetch failure is logged and swallowed: the worker still spawns with the un-enriched brief, and a missing
ticket body is not a reason to wedge the worker path, same spirit as poster.py's post-failure handling).
"""
import json, logging, os, re, subprocess

import site_env  # noqa: F401  (loads config/site.env before the BROKER_MCP_SH read below)

log = logging.getLogger("eph-broker.ticket_fetch")

MCP_SH = os.getenv("BROKER_MCP_SH", "")   # from site.env; unset means the fetch fails to exec (logged, None)
TOOL_NAME = "vikunja_task_crud"   # confirmed live via `mcp.sh vikunja-bridge tools/call`:
                                  # operation "get" + id returns the rendered ticket text (see fetch_ticket).

# Limitation: the rendered ticket text (title + HTML description) can run long on a ticket with lots of
# history/related-tasks rendering inline. Cap it so a pathological ticket can't blow up the worker's first
# prompt; if a real worker ever needs more, raise this rather than adding structured parsing.
MAX_TICKET_TEXT = 20_000


def build_call(task_id):
    """Pure builder: the mcp.sh `tools/call` JSON payload for reading a task. Unit-testable without
    executing anything. operation "get" + id is the confirmed live read shape.

    `id` is coerced to int: the bridge's schema types it `number`, and callers pass a string (app.py
    derives it from a "vikunja:task:<id>" key). Passing a string is rejected with -32602 BEFORE the tool
    runs, and that rejection comes back as an ordinary result envelope, not a top-level "error"."""
    return {
        "name": TOOL_NAME,
        "arguments": {
            "operation": "get",
            "id": int(task_id),
        },
    }


def build_list_call():
    """Pure builder: the mcp.sh `tools/call` payload for the allTasks LIST fallback.

    Why a fallback: `get` on a LABELLED ticket is BROKEN on the live vikunja-bridge: it renders the task's
    LABEL LIST ('**Results:** N item(s)' + label entries) instead of the task body (verified live),
    so `_extract_text` got a label-list blob with no title/description/done and the worker
    spawned contextless. The reliable recovery is the LIST rendering, which returns the
    full (untruncated) title+status+description for ANY ticket incl. labelled ones. `id` is NOT a filterable
    field in the Vikunja filter DSL (verified live: `id = N` gives 'Expected condition'), so the fallback fetches
    allTasks (auto-paged by the bridge) and matches the one task by its rendered '(ID: <n>)' client-side."""
    return {"name": TOOL_NAME, "arguments": {"operation": "list", "allTasks": True}}


def _extract_text(resp):
    """Pure classifier: pull the rendered ticket text out of an mcp.sh JSON-RPC response, or None on any
    unexpected shape. Confirmed live shape:
        {"result": {"content": [{"type": "text", "text": "<rendered markdown/HTML blob>"}]}}
    The `content[0].text` value is a human-readable RENDERING (title + HTML description inline), not
    structured JSON. Lazy-robust: return it VERBATIM rather than fragile-parsing individual fields out of
    it; that rendered blob already IS the ticket context the worker needs."""
    # `isError` is a TOP-LEVEL sibling of "result", the shape the bridge uses for a tool-level failure
    # (e.g. -32602 input validation), where content[0].text is the ERROR MESSAGE, not ticket text. Missing
    # this check is what let the MCP error get injected into worker briefs as if it were the ticket body.
    if not isinstance(resp, dict) or "error" in resp or resp.get("isError"):
        return None
    result = resp.get("result")
    if not isinstance(result, dict):
        return None
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return None
    text = content[0].get("text") if isinstance(content[0], dict) else None
    if not isinstance(text, str) or not text:
        return None
    # NO length cap here: the allTasks LIST fallback blob can run to hundreds of thousands of chars
    # on a busy tracker, so capping inside this classifier silently dropped every task past the first ~22, and
    # a HIGH-ID labelled ticket's block never survived truncation, so _reformat_list_task found nothing,
    # returned None, and the fail-closed check aborted the spawn silently. The fetch_ticket return paths each apply their
    # own [:MAX_TICKET_TEXT] cap, so the injected brief stays bounded; the search over the raw list needs
    # the FULL blob.
    return text


def _blob_has_task_body(text):
    """True iff a `get` blob is an ACTUAL ticket body, not the labelled-ticket LABEL-LIST bug. A real
    get-task blob carries the task's own fields (`**title:**`); the label-list rendering has only a header +
    '**Results:** N item(s)' + label entries and NO task fields. Injecting the label list as a brief is the
    contextless-spawn defect, so a get blob that lacks `**title:**` is treated as unusable and triggers the list fallback.

    Case-insensitive marker match: the bridge's field-label casing isn't guaranteed
    stable across renderings, so match `**title:**` regardless of case rather than trip the list fallback
    on a cosmetic casing change."""
    return bool(text) and "**title:**" in text.lower()


def _extract_blob_id(text):
    """Pull the ticket's OWN rendered id out of a `get` blob (its top-level `**id:** <n>` field line), or
    None if absent. Defense in depth: `fetch_ticket` cross-checks this against the REQUESTED task_id so
    a cross-wired mcp.sh response can't silently inject the WRONG ticket's body into a worker brief. mcp.sh's
    root cause (concurrent calls returning each other's JSON-RPC replies) was fixed at the source (per-bridge
    flock + unique JSON-RPC id); this is the belt-and-suspenders detector in case it ever regresses.

    The live get rendering emits the task's own id as a `**id:** 9324` field line (verified live;
    distinct from the `**taskId:**` request-echo and the `**identifier:** #42` project-relative form).
    Match the FIRST such line, line-anchored + case-insensitive, mirroring `_blob_has_task_body`'s marker style
    (`**taskId:**` does NOT match: the pattern requires the exact `**id:**` label). Pure."""
    if not text:
        return None
    m = re.search(r'(?im)^\s*\*\*id:\*\*\s*(\d+)', text)
    return int(m.group(1)) if m else None


def _reformat_list_task(list_text, task_id):
    """Pull ONE task's block out of an allTasks LIST rendering (matched by its '(ID: <task_id>)' header) and
    synthesize a GET-STYLE blob ('**title:** …' / '**done:** true|false' / '**description:** …') so the SAME
    downstream parsing that reads a get blob keeps working unchanged: the done-drop predicate (adapters/cc
    `_TICKET_DONE_RE` -> `**done:** true`) and the `repo:`-token parser (routing.parse_repo_token, which reads
    the HTML in the description). Returns the synthesized blob, or None if the task isn't found. Pure."""
    if not list_text:
        return None
    tid = int(task_id)
    marker = f"(ID: {tid})"
    # Blocks start at a '### N. **title** (ID: n)' header; split keeping each header with its body.
    for block in re.split(r'(?m)^(?=### \d+\. )', list_text):
        first = block.splitlines()[0] if block.strip() else ""
        if not (first.startswith("### ") and marker in first):
            continue
        m = re.search(r'\*\*(.*?)\*\*\s*\(ID:\s*%d\)' % tid, first)
        title = m.group(1).strip() if m else ""
        # ANCHORED to the Status FIELD line: the list rendering emits
        # '- **Status:** ✅ Done' (vs '❌ Not Done'). A bare `"✅ Done" in block` would misread a ticket
        # whose DESCRIPTION merely contains the string as done. Match only the line-anchored Status field,
        # case-insensitive; same spirit as adapters/cc `_TICKET_DONE_RE`.
        done = bool(re.search(r'(?im)^\s*-?\s*\*\*status:\*\*\s*✅\s*done\b', block))
        dm = re.search(r'(?s)- \*\*Description:\*\* (.*)', block)  # Description is the LAST field -> to block end
        desc = dm.group(1).strip() if dm else ""
        return (f"**title:** {title}\n"
                f"**done:** {'true' if done else 'false'}\n"
                f"**description:** {desc}")
    return None


def _call_mcp(call, mcp_sh, label, task_id):
    """Shell `mcp.sh vikunja-bridge tools/call <json>` and return the extracted content text (str) or None on
    ANY failure (logged, never raised); mcp.sh owns auth, no secrets touched here. `label` tags the log line
    (get vs list). timeout is generous (60s) because the list fallback auto-pages allTasks."""
    try:
        out = subprocess.run(
            [mcp_sh, "vikunja-bridge", "tools/call", json.dumps(call)],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("ticket_fetch: mcp.sh exec failed (%s) task_id=%s: %s", label, task_id, e)
        return None
    if out.returncode != 0:
        log.warning("ticket_fetch: mcp.sh exit=%d (%s) task_id=%s stderr=%s",
                    out.returncode, label, task_id, (out.stderr or "").strip()[:500])
        return None
    try:
        resp = json.loads(out.stdout or "{}")
    except (ValueError, TypeError) as e:
        log.warning("ticket_fetch: mcp.sh non-JSON response (%s) task_id=%s: %s", label, task_id, e)
        return None
    return _extract_text(resp)


def fetch_ticket(task_id, mcp_sh=MCP_SH):
    """Fetch a Vikunja ticket's rendered content, ROBUST against the labelled-ticket `get` bridge bug.
    Returns the ticket text (str) on success, or None on ANY failure (logged, never
    raised); the worker path fail-closes on None (adapters/cc._run_worker); mcp.sh owns auth.

    PRIMARY: `get` (one fast call, correct field format). Reliable for UNLABELLED tickets. FALLBACK (only when
    `get` returns the label-list bug / an unusable body): the allTasks LIST rendering, reformatted into the
    same get-style field blob so downstream parsing is unchanged. See build_list_call / _reformat_list_task."""
    if task_id is None:
        log.info("ticket_fetch: no task_id -> skipping fetch")
        return None
    try:
        tid = int(task_id)                  # int() coercion can raise on a non-numeric id; never wedge a launch
    except (TypeError, ValueError):
        log.warning("ticket_fetch: non-numeric task_id=%r -> skipping fetch", task_id)
        return None
    text = _call_mcp(build_call(tid), mcp_sh, "get", tid)
    if _blob_has_task_body(text):
        # Defense in depth: a bodied get blob is only trustworthy if its OWN rendered id matches the id
        # we asked for. On a match, return it. On a MISMATCH (or an id that can't be extracted from an
        # otherwise-bodied blob) the get result is UNUSABLE: mcp.sh may have cross-wired concurrent calls
        # (fixed at the source) and handed us ANOTHER ticket's body. Reject it and fall
        # through to the ID-matched list fallback (which matches by '(ID: <tid>)', so it recovers the RIGHT
        # ticket). ID extraction runs on the FULL untruncated blob (the [:MAX_TICKET_TEXT] cap is applied
        # only on the return). Detection before correction: log the mismatch loudly.
        blob_id = _extract_blob_id(text)
        if blob_id == tid:
            log.info("ticket_fetch: fetched task_id=%s via get len=%d", tid, len(text))
            return text[:MAX_TICKET_TEXT]
        log.warning("ticket_fetch: get blob id-mismatch requested task_id=%s but blob id=%r "
                    "-> REJECT (cross-wired mcp.sh?), falling through to ID-matched list fallback", tid, blob_id)
    else:
        # `get` returned the labelled-ticket LABEL-LIST bug (or nothing usable).
        log.warning("ticket_fetch: get had no task body task_id=%s (labelled-ticket bridge bug) -> list fallback", tid)
    # Reliable, ID-matched recovery: the allTasks LIST rendering, reformatted into the same get-style field blob.
    blob = _reformat_list_task(_call_mcp(build_list_call(), mcp_sh, "list", tid), tid)
    if blob is None:
        log.warning("ticket_fetch: list fallback could not resolve task_id=%s -> None (fail-closed upstream)", tid)
        return None
    log.info("ticket_fetch: fetched task_id=%s via list fallback len=%d", tid, len(blob))
    return blob[:MAX_TICKET_TEXT]


# Who a ticket belongs to, read from a `get` blob. The blob renders
# `**assignees:** [ {..."username": "dev"...} ]` (omitted entirely when unassigned) and `**created_by:** {...}`
# as multi-line JSON. A description can carry a lookalike, so ticket_meta FAILS CLOSED on any
# duplicate field / truncated / list-fallback blob instead of picking an occurrence.
# CONTRACT (anti-spoof depends on it): vikunja-bridge's `get` view renders `description`, then `done`, then
# `assignees`/`labels`/`created_by`. If the bridge reorders fields, ticket_meta fails closed; re-check then.
_FIELD_RE = r'(?ims)^\*\*{}:\*\*(.*?)(?=^\*\*\w+:\*\*|\Z)'
_USERNAME_RE = re.compile(r'"username":\s*"([^"]*)"')


def _field_starts(text, name):
    """Offsets of every line-anchored `**<name>:**` field line in `text` (case-insensitive). Pure."""
    return [m.start() for m in re.finditer(r'(?im)^\*\*%s:\*\*' % re.escape(name), text)]


def ticket_meta(text):
    """The ONE trusted read of a ticket's ownership/labels off a fetched blob, shared by every caller
    that makes a trust decision on them (comment rerun, comment respawn, the `model:` label).
    -> (assignee usernames lowercased, creator username lowercased, label titles), or None (FAIL CLOSED)
    whenever the blob can't be trusted to carry the REAL fields rather than a description lookalike:
      * empty / possibly TRUNCATED (len >= MAX_TICKET_TEXT: fetch_ticket caps there, so the real trailing
        created_by/labels may have been cut and a lookalike in the description would be all that's left);
      * not get-shaped: a get blob renders `description` -> `done` -> ... -> assignees/labels/created_by,
        so exactly one `**done:**` must follow the (at most one) `**description:**`, and every metadata
        field must follow that `**done:**`. The list-fallback blob (_reformat_list_task) renders description
        LAST, after done, so any field there is description text -> rejected;
      * any field duplicated (more than one created_by/assignees/labels/description/done line; a lookalike
        in the description makes a second one), or created_by missing / without a username.
    Pure. (A structured JSON fetch would be stronger, but vikunja-bridge only exposes the rendered view.)"""
    if not text or len(text) >= MAX_TICKET_TEXT:
        return None
    desc, done = _field_starts(text, "description"), _field_starts(text, "done")
    creators = _field_starts(text, "created_by")
    assignees, labels = _field_starts(text, "assignees"), _field_starts(text, "labels")
    if len(done) != 1 or len(desc) > 1 or len(creators) != 1 or len(assignees) > 1 or len(labels) > 1:
        return None
    if desc and desc[0] > done[0]:
        return None
    if any(pos < done[0] for pos in creators + assignees + labels):
        return None
    names = _USERNAME_RE.findall(re.findall(_FIELD_RE.format("created_by"), text)[0])
    if not names:
        return None
    a_blk = re.findall(_FIELD_RE.format("assignees"), text)
    l_blk = re.findall(_FIELD_RE.format("labels"), text)
    return ([u.lower() for u in _USERNAME_RE.findall(a_blk[0])] if a_blk else [], names[0].lower(),
            _TITLE_RE.findall(l_blk[0]) if l_blk else [])


def ticket_field(text, name):
    """The trusted single-line value (str, stripped) of a scalar field like `done` / `project_id` off a
    fetched blob, or None (FAIL CLOSED) when the real field can't be told apart from a description lookalike.
    Same anchoring contract as ticket_meta (never a first/last regex match on the rendered blob):
      * list-fallback shape (`**done:**` precedes `**description:**`, which runs to the blob end): only the
        FIRST `**done:**` (before the description) is real; no other field exists there, so None;
      * get shape: blob not possibly-truncated (len < MAX_TICKET_TEXT), exactly one `**done:**`, at most one
        `**description:**` and it precedes done, and the requested field occurs EXACTLY once, at/after done
        (a lookalike in the description makes a duplicate or sits before done -> None). Pure."""
    if not text:
        return None
    name = name.lower()
    done, desc = _field_starts(text, "done"), _field_starts(text, "description")
    if not done:
        return None
    if desc and done[0] < desc[0]:                        # list-fallback blob: description is LAST
        pos = done[0] if name == "done" else None
    else:
        if len(text) >= MAX_TICKET_TEXT or len(done) != 1 or len(desc) > 1:
            return None
        hits = _field_starts(text, name)
        pos = hits[0] if len(hits) == 1 and hits[0] >= done[0] else None
    if pos is None:
        return None
    line = text[pos:].split("\n", 1)[0]
    return line[len("**%s:**" % name):].strip()


def ticket_done(text):
    """True IFF the trusted `done` field (ticket_field) is `true` (case-insensitive). FAIL-SAFE toward
    NOT done: None/unfetched/absent/false/duplicated/lookalike all give False, so a description carrying a fake
    `**done:** true` line can never drop a spawn. Pure."""
    v = ticket_field(text, "done")
    return bool(v) and v.split()[0].lower() == "true"


def ticket_people(text):
    """-> (assignee usernames, creator username), lowercased. (None, None) when ticket_meta can't trust the
    blob (fetch failed, list-fallback blob, truncated, duplicated/lookalike fields); callers fail closed. Pure."""
    meta = ticket_meta(text)
    return (None, None) if meta is None else (meta[0], meta[1])


# A ticket's LABELS, read from the same `get` blob the same way ticket_people reads
# assignees/created_by: the live rendering emits `**labels:** [ {... "title": "eph:code-change" ...} ]`
# (Vikunja's Label object names its text field `title`, not `username`), omitted entirely when the ticket
# carries no label (mirrors the assignees contract). Used by app._comment_respawn (via
# routing.comment_respawn_domain) to classify a ticket by LABEL when the respawn-triggering comment event
# itself carries no label signal at all.
_TITLE_RE = re.compile(r'"title":\s*"([^"]*)"')


def ticket_labels(text):
    """-> label titles (list[str]), off a `get` blob's **labels:** field. [] when the ticket carries no
    label / the field is absent, AND when ticket_meta can't trust the blob: a lookalike label in a
    truncated/list-fallback/duplicated blob is never honored. Pure."""
    meta = ticket_meta(text)
    return [] if meta is None else meta[2]
