# Your own agent types (routes.json)

The broker ships with a fixed set of rows in `routing.py`. You can add your own in `config/routes.json`
without touching code. Each row says which tickets it picks up, which signed prompt the worker runs, and
which of the built-in capabilities it gets. Start from `config/routes.example.json`. The same rows can go in the
`routes:` list of `config/broker.yaml` instead ([configuration.md](configuration.md#the-config-file)).

```json
{
  "routes": [
    {
      "id": "docs",
      "match": {"label": "eph:docs"},
      "prompt": "docs/writer",
      "capability": "code",
      "repo": "/srv/git/docs-site"
    }
  ]
}
```

## Fields

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Row name. Lowercase letters, digits and dashes, up to 40 characters. Can't reuse a built-in row's name. |
| `match` | yes | `{"label": "..."}`, `{"assignee": "..."}` or both. Matching is exact and case-insensitive. With both, the ticket needs the assignee and the label. |
| `prompt` | yes | `<domain>/<class>`, which means `prompts/<domain>/<class>.md`. It has to be signed in `approved-prompts.json` already. |
| `capability` | yes | `read`, `code` or `general` (below), or one of your own [site capabilities](#site-capabilities). |
| `repo` | yes | Absolute path to the repo the worker gets a throwaway worktree of. It has to be in `CODE_REPO_ALLOWLIST` or `GENERAL_REPO_ALLOWLIST` (see below). |
| `agent` | no | Name passed to `FLEET_MODEL_SH` to pick a model. Defaults to the built-in worker of the same capability (`CODE_AGENT`, `GENERAL_AGENT`, or the ansible row's agent for `read`). |
| `model` | no | Pin a model for this row (for example `opus`), skipping `FLEET_MODEL_SH`. |

Keys starting with `_` are ignored, so you can leave `_comment` notes.

## Capabilities

The tool grants for each built-in capability are defined in `adapters/cc.py` and a config row can't change them;
a [site capability](#site-capabilities) can only add rules on top. There is no field for tools, permissions,
publish targets or the harness on a row, and an unknown key is an error. Config rows run on the default Claude Code harness; see
[adapters.md](adapters.md#how-a-route-picks-its-harness) for the one label that can move a ticket to hermes.

- `read`: read-only tools (read, search, list) in a worktree of `repo`. The result is a ticket comment.
  `repo` must be in either allowlist.
- `code`: the same grant as the built-in code worker. It can edit files, commit and run the repo's test
  runner, and the broker pushes a branch named `eph/<id>/...` and opens an MR. `repo` must be in
  `CODE_REPO_ALLOWLIST`.
- `general`: the same grant and MCP config as the built-in general worker. `repo` must be in
  `GENERAL_REPO_ALLOWLIST`.

The other capabilities (wiki, inbox, MR review and so on) depend on code specific to their row, so a config
row can't use them. Prompt domains the broker treats specially by name (`tfa`, `inbox`, `infra`) are also
refused.

## Site capabilities

The `capabilities:` section of `config/broker.yaml` defines your own capabilities. Each one is a built-in base
plus extra permission rules, written the way Claude Code writes them (`Read`, `Bash(wc -w:*)`,
`mcp__server__tool`):

```yaml
capabilities:
  docs-writer:
    base: code                 # read, code or general
    allow: ["Bash(wc -w:*)"]   # added to the base's --allowedTools
    deny: [WebFetch]           # added to the base's --disallowedTools
```

A row that names `docs-writer` runs exactly like a `code` row (same allowlist, plumbing, limits in
`constraints` and backend pin), and the worker's argv is the `code` grant with these rules appended. The base's
own deny list stays as it is and a deny always wins, so an `allow` can't lift something the base denies; it
only does anything for a tool the base doesn't already deny. An `allow` rule is a security decision, the same as
signing a prompt: read it before you add it.

The broker refuses to start when a name reuses a built-in capability, the base isn't `read`, `code` or
`general`, a rule is malformed, `general` gets an `allow` (an allowed tool skips the auto-mode classifier), or
an `allow` admits every shell command (`Bash`, `Bash(*)`, `Bash(:*)`, or a shell, interpreter or command runner
such as `Bash(bash:*)`, `Bash(python3.11:*)`, `Bash(tclsh8.6:*)`, `Bash(npx:*)`, `Bash(npm x:*)` or
`Bash(env:*)`, also when quoted or behind `VAR=value`, `command` or `builtin`, or a wrapper like `stdbuf`,
`ionice`, `doas` or `systemd-run`, or a script runner like `make`, `npm run`, `poetry run`, `tox` or
`cargo run`). A first word with `$`, a glob, `{` or `~` in it is refused too. That list is best effort: it catches the
obvious cases, not every way a command can run another one, and it's there to stop mistakes, not a hostile
config (`broker.yaml` is yours and trusted). So name exact commands. Anything that runs files the worker can
edit is as good as a shell on a code worker: `make` runs the Makefile, `npm run` runs package.json, a test
runner runs the tests, and the worker can change all of them in its worktree. `find`, `sed` and `git` aren't
refused, but they're as good as a shell too (`find -exec`, sed's `e` command, git hooks and aliases), so only
allow them with fixed arguments. The test-runner commands a code row needs (`npm test`, `npm start`,
`yarn <script>`, `cargo build`, `cargo test` and the like) aren't refused either, since a code worker can't do
its job without them. They run files in the worktree too, so treat them as a known gap, not a boundary. A ticket's `hermes` label is refused on a site capability, because hermes has
no allow and deny lists to add the rules to. The section is read once at startup.

## Where a row works

A config row always works in its own `repo`. A `repo:` line in the ticket that names a different repo makes
the broker refuse the ticket, and the repo inference used by the built-in code worker is skipped. The
allowlist is checked again at every spawn.

A row also inherits the built-in worker's backend pin (`CODE_CHANGE_BACKEND`, `GENERAL_BACKEND`, or
`ANSIBLE_BACKEND` for `read`), so a site-wide pin applies to your rows too.

## Ordering

Config rows are checked after all the built-in label and assignee rows and before the general fallbacks
(the `eph` assignee, the launcher assignee if you set one, and the `eph:general` label). A ticket labelled `eph:code-change`
still goes to the built-in code worker even if it also carries one of your labels. Among config rows the first
match in file order wins.

So that every row can actually run, the broker refuses a file where a row's own label or assignee is also
matched by a built-in row that is on, or by an earlier config row. Some built-in rows match by substring: with
`WIKI_ROW_LABEL=wiki` set, a label like `wiki-audit` or an assignee like `wikibot` is refused because the
built-in wiki row would take those tickets. A config row can't claim the fallback triggers either (the `eph`
assignee, the launcher assignee and the `eph:general` label). If you have a narrow row and a broad one on the same label, list the narrow one (assignee and label)
first.

## Loading and signing

The file is read once at startup. Restart the broker after you change it; `SIGHUP` doesn't reload it.

Every problem stops the broker from starting, with a message naming the row and the field. That includes
an unknown capability, an unsigned prompt, a prompt edited since it was signed, a repo outside the allowlist,
a label or assignee another row already matches, and a malformed file. Nothing is half-loaded. To check a
file before a restart:

```sh
ROUTES_PATH=config/routes.json python3 -c "import routing"
```

The prompt check runs again at every spawn, so editing a prompt after startup without re-signing it stops
that row's workers from starting rather than running the new text.

Without a `routes.json` the routing table is exactly the built-in one.
