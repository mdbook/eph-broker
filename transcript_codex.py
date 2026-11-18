"""transcript_codex.py: pure Codex item projection shared with the broker, without MCP payloads."""
from transcript_redaction import _redact_secretish


def item_detail(item, cwd=None, snaps=None, file_diffs=None):
    """What a finished item said or did, for the live transcript: message and reasoning-summary text, the command
    line with its exit code and the tail of its output, edited files with their diffs, plan items, MCP server/tool
    names and web queries. Never MCP arguments or results. Each string is redacted whole (the same secret patterns
    as the cc adapter, which records full tool output), then capped."""
    red = lambda v, cap: _redact_secretish(v)[:cap] if isinstance(v, str) and v else None   # redact, then cap
    t, out = item.get("type"), {}
    if t in ("agent_message", "reasoning"):
        out["text"] = red(item.get("text"), 4000)
    elif t == "command_execution":
        out["command"] = red(item.get("command"), 2000)
        if isinstance(item.get("exit_code"), int):
            out["exit_code"] = item["exit_code"]
        o = item.get("aggregated_output")
        if isinstance(o, str) and o.strip():
            o = _redact_secretish(o.rstrip("\n"))
            out["output_lines"] = o.count("\n") + 1
            out["output"] = o if len(o) <= 3000 else "..." + o[-3000:]   # the tail is where errors and results are
    elif t == "file_change" and isinstance(item.get("changes"), list):
        out["paths"] = [p for p in (red(c.get("path"), 300) for c in item["changes"][:20] if isinstance(c, dict)) if p]
        out["files"] = file_diffs(item["changes"], cwd, snaps if snaps is not None else {}) if file_diffs else []
    elif t == "mcp_tool_call":
        out["tool"] = red(".".join(x for x in (item.get("server"), item.get("tool")) if isinstance(x, str)), 160)
    elif t == "todo_list" and isinstance(item.get("items"), list):
        out["todo"] = [{"text": red(i.get("text"), 300), "done": i.get("completed") is True}
                       for i in item["items"][:30] if isinstance(i, dict) and red(i.get("text"), 300)]
    elif t == "web_search":
        out["query"] = red(item.get("query"), 300)
    return {k: v for k, v in out.items() if v not in (None, [])}
