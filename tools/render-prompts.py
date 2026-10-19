#!/usr/bin/env python3
"""Render eph-broker worker system-prompts from modules/ + a per-class manifest.

Build-time authoring layer: composes modules/**.md fragments (selected + ordered by a
workers/<domain>/<class>.json manifest) with {{var}} stdlib string-replace, and writes the
rendered prompt to prompts/<domain>/<class>.md, the exact path the broker already loads and
sha256-gates. The broker (prompts.py/cc.py) is untouched; per-ticket data still rides the -p brief.

A section may also be a dict {"inline": "<abs path>", "heading": "...", "strip_frontmatter": bool}:
that external doc is inlined VERBATIM (no {{var}} replace), under `heading`. Rendering records each
source's sha256 in prompts/<domain>/<class>.sources.json; --check fails when a source has changed since (re-render
and re-sign). A manifest whose inline source is unreadable (e.g. in CI) is skipped by --check.

modules/site/<section>.md, when present, replaces modules/<section>.md: a deployment keeps its own wording
(tools, memory, review rules) there while modules/ stays generic. The public export drops modules/site/.

Usage:
  render-prompts.py                 # render every workers/**/*.json into prompts/**
  render-prompts.py --check         # render to memory, fail (exit 1) if any committed prompt drifts
Stdlib only, {{var}} replace, no template engine: express variation by module selection.
"""
import sys, re, json, hashlib, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SLOT = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.S)


def strip_frontmatter(text):
    """Drop a leading YAML frontmatter block (--- ... ---) and the blank lines after it."""
    return _FRONTMATTER.sub("", text, count=1).lstrip("\n")


def inline_sources(manifest_path: pathlib.Path):
    """The absolute paths a manifest inlines, in section order."""
    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    return [sec["inline"] for sec in m["sections"] if isinstance(sec, dict)]


def sources_path(out: pathlib.Path):
    return out.with_name(out.stem + ".sources.json")


def source_hashes(paths):
    return {p: hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest() for p in paths}


def render(manifest_path: pathlib.Path):
    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    vars_ = m.get("vars", {})
    parts = []
    for sec in m["sections"]:
        if isinstance(sec, dict):
            text = pathlib.Path(sec["inline"]).read_text(encoding="utf-8")
            if sec.get("strip_frontmatter"):
                text = strip_frontmatter(text)
            text = text.strip("\n")
            parts.append(f"{sec['heading']}\n\n{text}" if sec.get("heading") else text)
            continue
        site = ROOT / "modules" / "site" / f"{sec}.md"
        text = (site if site.exists() else ROOT / "modules" / f"{sec}.md").read_text(encoding="utf-8")
        def sub(mo):
            k = mo.group(1)
            if k not in vars_:
                raise SystemExit(f"{manifest_path.name}: module {sec} needs var '{k}' not in manifest")
            return str(vars_[k])
        parts.append(SLOT.sub(sub, text).rstrip("\n"))
    out = ROOT / "prompts" / m["domain"] / f"{m['class']}.md"
    return out, "\n\n".join(parts) + "\n"


def check_sources(out: pathlib.Path, paths):
    """[problem, ...] for every inline source whose sha256 differs from the recorded one."""
    sp = sources_path(out)
    recorded = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
    problems = []
    for p, now in source_hashes(paths).items():
        if recorded.get(p) != now:
            problems.append(f"SOURCE CHANGED: {p} (recorded {recorded.get(p)}, now {now}) - re-render "
                            f"{out.relative_to(ROOT)} and have the operator re-sign it")
    return problems


def main():
    check = "--check" in sys.argv[1:]
    drift = 0
    for mf in sorted((ROOT / "workers").rglob("*.json")):
        srcs = inline_sources(mf)
        missing = [p for p in srcs if not pathlib.Path(p).is_file()]
        if missing:
            if check:
                print(f"skip: {mf.relative_to(ROOT)} (inline source unavailable here: {', '.join(missing)})")
                continue
            raise SystemExit(f"{mf.relative_to(ROOT)}: inline source missing: {', '.join(missing)}")
        out, rendered = render(mf)
        if check:
            have = out.read_text(encoding="utf-8") if out.exists() else None
            problems = check_sources(out, srcs)
            for p in problems:
                print(p)
            if have != rendered:
                print(f"DRIFT: {out.relative_to(ROOT)} does not match render of {mf.relative_to(ROOT)}")
                drift = 1
            elif problems:
                drift = 1
            else:
                print(f"ok: {out.relative_to(ROOT)}")
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(rendered, encoding="utf-8")
            if srcs:
                sources_path(out).write_text(json.dumps(source_hashes(srcs), indent=2) + "\n", encoding="utf-8")
            print(f"rendered: {out.relative_to(ROOT)}")
    sys.exit(drift)

if __name__ == "__main__":
    main()
