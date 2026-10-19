#!/usr/bin/env python3
"""repo_infer.py: pick a code ticket's target repo when it has no `repo:` line.

Decision ladder. Every step has a timeout; any failure
falls through to ASK, never a silent guess.

  1. explicit `repo:` line                    (handled by the caller: adapters/cc.py _code_repo_ladder)
  2. strong deterministic name/alias match    word-boundary; a single common word (docker/build/wiki/...) or a
                                              <=3-char name is WEAK only; `ns/name` spelled out is always strong
  3. Laya over a lexical top-8 shortlist      accept iff top >= 0.6 AND top - second >= 0.3
  4. 4B (qwen3-4b-classifier) over the list   accepted ONLY if its pick is in Laya's top-2 (its self-reported
                                              confidence is not trustworthy); skipped when Laya gave nothing
  5. ask                                      top-3 candidates; the caller comments + applies the parked label

Tier B (control plane) is reachable only via step 1 or an EXACT-name match in step 2 (the title's bare name, or
`ns/name` spelled out anywhere). A model pick, an alias hit, or a bare name in the description into tier B is
refused and the ladder asks (a title alias into tier B, e.g. an infra-repo subproject, asks at once with that repo
first and a one-line `repo:` confirm). DENY repos are not in the catalog's reachable set at all.

Pure where it matters: `infer(text, catalog, laya=..., qwen=...)` takes its model clients as arguments so the
tests mock them. The real clients are stdlib-only (unix socket / urllib) with hard timeouts.
"""
import html as _html, json, logging, os, re, socket, threading, time, urllib.request
from dataclasses import dataclass, field

import site_env  # noqa: F401  (loads config/site.env before the REPO_INFER_* reads below)

log = logging.getLogger("eph-broker.repo_infer")

WEAK_NAMES = set(x.strip().lower() for x in os.getenv(
    "REPO_WEAK_NAMES", "docker,build,scripts,agents,wiki,ops,fleet").split(",") if x.strip())
SHORTLIST_K = int(os.getenv("REPO_INFER_SHORTLIST_K", "8"))
LAYA_SOCK = os.path.expanduser(os.getenv("LAYA_PICK_SOCK", "~/.eph-broker/laya-pick.sock"))
LAYA_TIMEOUT_S = float(os.getenv("REPO_INFER_LAYA_TIMEOUT", "20"))
LAYA_TOP = float(os.getenv("REPO_INFER_LAYA_TOP", "0.6"))
LAYA_MARGIN = float(os.getenv("REPO_INFER_LAYA_MARGIN", "0.3"))
LAYA_TEXT_CHARS = int(os.getenv("REPO_INFER_LAYA_TEXT_CHARS", "1000"))
QWEN_URL = os.getenv("REPO_INFER_4B_URL", "")   # from site.env; "" means the 4B rung raises and the ladder asks
QWEN_MODEL = os.getenv("REPO_INFER_4B_MODEL", "qwen3-4b-classifier")
QWEN_TIMEOUT_S = float(os.getenv("REPO_INFER_4B_TIMEOUT", "45"))
QWEN_MAX_TOKENS = int(os.getenv("REPO_INFER_4B_MAX_TOKENS", "600"))
QWEN_TEXT_CHARS = int(os.getenv("REPO_INFER_4B_TEXT_CHARS", "2000"))
SHADOW_LOG = os.path.expanduser(os.getenv("REPO_INFER_SHADOW_LOG", "~/.cache/eph-broker/repo-infer-shadow.jsonl"))


@dataclass
class Decision:
    action: str                       # "pick" | "ask"
    repo: object = None               # repo_catalog.Repo on pick
    step: str = ""                    # "token" | "name" | "laya" | "4b" | "ask"
    reason: str = ""
    candidates: list = field(default_factory=list)   # repo paths, best first (<=3); shown on ask

    def summary(self):
        return {"action": self.action, "repo": getattr(self.repo, "path", None), "step": self.step,
                "reason": self.reason, "candidates": self.candidates}


# ---------------------------------------------------------------- text
_FIELD_RE = r"(?im)^\s*-?\s*\*\*{0}:\*\*\s*"


def inference_text(ticket_text):
    """Title (first line) + description out of the broker's rendered ticket blob (ticket_fetch format), HTML stripped,
    every bare `repo:` line removed (so a shadow run can't cheat off the token). Falls back to the whole
    blob when the field markers are absent (issue-code text / tests)."""
    t = str(ticket_text or "")
    m_title = re.search(_FIELD_RE.format("title") + r"(.*)$", t)
    m_desc = re.search(r"(?s)" + _FIELD_RE.format("description") + r"(.*?)(?=^\s*-?\s*\*\*[a-z_]+:\*\*|\Z)", t)
    if m_title or m_desc:
        t = ((m_title.group(1) if m_title else "").strip() + "\n" + (m_desc.group(1) if m_desc else ""))
    t = re.sub(r"(?i)<br[^>]*>|</?(p|div|li|ul|ol|h\d)[^>]*>", "\n", t)
    t = _html.unescape(re.sub(r"<[^>]+>", " ", t)).replace("\xa0", " ")
    t = "\n".join(line for line in t.splitlines() if not line.strip().lower().startswith("repo:"))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", t)).strip()


def _tok(s):
    return set(w for w in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(w) > 2)


def _mentions(text_lc, needle):
    return re.search(r"(?<![a-z0-9_-])" + re.escape(needle) + r"(?![a-z0-9_-])", text_lc) is not None


def name_hits(text, catalog):
    """path -> "name" (exact name / full path: strong, tier-B-eligible), "alias" (curated alias: strong but
    NOT tier-B-eligible), or "weak" (common single word / <=3 chars: a shortlist hint only)."""
    t = (text or "").lower()
    hits = {}
    for r in catalog.repos:
        name = r.name.lower()
        if _mentions(t, r.path.lower()) or ("-" in name and _mentions(t, name.replace("-", "_"))):
            hits[r.path] = "name"                     # `ns/name` spelled out, or a `game_sync` package path
        elif _mentions(t, name):
            hits[r.path] = "weak" if (name in WEAK_NAMES or len(name) <= 3) else "name"
        elif any(_mentions(t, a) for a in r.aliases):
            hits[r.path] = "alias"
    return hits


def _strong_pick(hits, catalog):
    """Exactly one strong hit (after dropping names contained in a longer hit, e.g. `agents` inside
    `coder-agent`...) gives (repo, kind). Ambiguous or none gives (None, None)."""
    strong = [p for p, k in hits.items() if k in ("name", "alias")]
    strong = [p for p in strong
              if not any(o != p and catalog.by_path[p].name.lower() in catalog.by_path[o].name.lower()
                         for o in strong)]
    if len(strong) != 1:
        return None, None
    return catalog.by_path[strong[0]], hits[strong[0]]


def shortlist(text, catalog, k=SHORTLIST_K, hits=None):
    """Lexical top-k (probe: right repo in the top-8 10/10). Name/alias hits are forced to the front."""
    hits = name_hits(text, catalog) if hits is None else hits
    tt = _tok(text)
    bonus = {"name": 100, "alias": 100, "weak": 5}

    def score(r):
        return (bonus.get(hits.get(r.path), 0) + 3 * len(tt & _tok(r.name.replace("-", " ")))
                + len(tt & _tok(r.card)) + 2 * sum(1 for a in r.aliases if _tok(a) & tt))
    return sorted(catalog.repos, key=lambda r: (-score(r), r.path))[:k]


# ---------------------------------------------------------------- model clients (real)
def laya_pick(text, options, timeout=LAYA_TIMEOUT_S, sock_path=None):
    """Ask the broker-owned Laya pick daemon (tools/laya_pick_daemon.py). options: {name: card}.
    Returns {name: prob}. Raises on ANY failure (no daemon / timeout / error reply), so the caller asks."""
    deadline = time.time() + timeout
    req = json.dumps({"verb": "pick", "text": text[:LAYA_TEXT_CHARS], "options": options}) + "\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
        c.settimeout(timeout)
        c.connect(sock_path or LAYA_SOCK)
        c.sendall(req.encode())
        buf = b""
        while not buf.endswith(b"\n"):
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError("laya pick timed out")
            c.settimeout(left)
            chunk = c.recv(65536)
            if not chunk:
                break
            buf += chunk
    out = json.loads(buf)
    if "error" in out or not isinstance(out.get("probs"), dict):
        raise RuntimeError(f"laya pick error: {str(out.get('error'))[:200]}")
    return {str(k): float(v) for k, v in out["probs"].items()}


def qwen_pick(text, repos, timeout=QWEN_TIMEOUT_S):
    """Ask the 4B over the full list (probe4b prompt). Returns the picked short name or None ("NONE").
    Raises on transport/parse failure, so the caller asks."""
    lst = "\n".join(f"- {r.name}: {r.card}" if r.card else f"- {r.name}" for r in repos)
    msg = ("Pick the ONE code repository this ticket should be worked in. Repos:\n" + lst +
           "\n\nTicket: " + text[:QWEN_TEXT_CHARS] +
           '\n\nAnswer ONLY JSON: {"repo": "<name or NONE>", "confidence": 0-100, "runner_up": "<name>"}')
    body = json.dumps({"model": QWEN_MODEL, "messages": [{"role": "user", "content": msg}],
                       "temperature": 0, "max_tokens": QWEN_MAX_TOKENS}).encode()
    req = urllib.request.Request(QWEN_URL, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)
    content = resp["choices"][0]["message"].get("content") or ""
    objs = re.findall(r'\{[^{}]*"repo"[^{}]*\}', content)
    if not objs:
        raise ValueError("4B reply carried no JSON answer")
    pick = str(json.loads(objs[-1]).get("repo") or "").strip()
    return None if not pick or pick.upper() == "NONE" else pick


# ---------------------------------------------------------------- the ladder (steps 2-5)
def _ask(reason, candidates):
    return Decision("ask", None, "ask", reason, [p for p in candidates][:3])


def _tier_b_alias_ask(repo, title, catalog, text, hits):
    """Ask naming the tier-B repo an alias points at (plus the subproject dir, when generated)."""
    t = (title or "").lower()
    alias = next((a for a in repo.aliases if _mentions(t, a)), "")
    label = repo.local or repo.name
    where = getattr(repo, "subprojects", {}).get(alias) or alias
    rest = [r.path for r in shortlist(text, catalog, hits=hits) if r.path != repo.path]
    return _ask(f"This looks like {label} (`{where}`). {label} is a control-plane repo, so confirm with a "
                f"`repo: {repo.path}` line.", [repo.path] + rest)


def infer(text, catalog, laya=None, qwen=None):
    """Steps 2-5 of the ladder over already-extracted ticket text (see inference_text)."""
    laya = laya_pick if laya is None else laya
    qwen = qwen_pick if qwen is None else qwen
    if not catalog.repos:
        return _ask("repo catalog is empty", [])
    # The TITLE naming exactly one repo wins over incidental description mentions; else the whole text must
    # name exactly one (two different strong names = ambiguous, so the model decides, with both shortlisted).
    # Past the title, a tier-B bare name ("the ansible cron ...") is incidental, so weak; only the
    # spelled-out `ns/name` keeps a tier-B repo strong there.
    text_lc = (text or "").lower()
    hits = {p: ("weak" if k in ("name", "alias") and catalog.is_tier_b(catalog.by_path[p])
                and not _mentions(text_lc, p.lower()) else k)
            for p, k in name_hits(text, catalog).items()}
    title = (text or "").split("\n", 1)[0]
    repo, kind = _strong_pick(name_hits(title, catalog), catalog)
    if repo is None:
        repo, kind = _strong_pick(hits, catalog)
    if repo is not None:
        if kind == "name" or not catalog.is_tier_b(repo):
            return Decision("pick", repo, "name", f"ticket names `{repo.name}`" if kind == "name"
                            else f"ticket mentions alias of `{repo.name}`")
        # an alias into tier B is not an exact name: never a pick. It is still the best lead (e.g. a
        # /srv/infra subproject named in the title), so ask with it first and a one-line confirm.
        return _tier_b_alias_ask(repo, title, catalog, text, hits)
    sl = shortlist(text, catalog, hits=hits)
    ranked = []
    try:
        probs = laya(text, {r.name: (r.card or r.name.replace("-", " "))[:60] for r in sl})
        ranked = sorted(((n, p) for n, p in probs.items() if n in catalog.by_name), key=lambda kv: -kv[1])
    except Exception as e:
        log.warning("repo infer: Laya unavailable (%s: %s) -> no model pick", type(e).__name__, str(e)[:160])
    cands = [catalog.by_name[n].path for n, _ in ranked] or [r.path for r in sl]
    if ranked:
        top_n, top_p = ranked[0]
        second_p = ranked[1][1] if len(ranked) > 1 else 0.0
        if top_p >= LAYA_TOP and top_p - second_p >= LAYA_MARGIN:
            pick = catalog.by_name[top_n]
            if catalog.is_tier_b(pick):
                return _ask(f"best guess `{pick.name}` is a control-plane repo - those need an explicit "
                            f"repo: line", cands)
            return Decision("pick", pick, "laya", f"Laya p={top_p:.2f}, margin {top_p - second_p:.2f}", cands[:3])
    else:
        return _ask("no confident name match and the repo classifier was unavailable", cands)
    try:
        q = qwen(text, catalog.repos)
    except Exception as e:
        log.warning("repo infer: 4B unavailable (%s: %s)", type(e).__name__, str(e)[:160])
        return _ask(f"low confidence (Laya top p={ranked[0][1]:.2f}) and the 4B cross-check was unavailable",
                    cands)
    top2 = [n for n, _ in ranked[:2]]
    if q in top2:
        pick = catalog.by_name[q]
        if catalog.is_tier_b(pick):
            return _ask(f"best guess `{pick.name}` is a control-plane repo - those need an explicit repo: line",
                        cands)
        return Decision("pick", pick, "4b", f"4B picked `{q}`, agreeing with Laya's top-2 {top2}", cands[:3])
    return _ask(f"low confidence: Laya top-2 {top2}, 4B said `{q}`", cands)


# ---------------------------------------------------------------- shadow mode
def shadow_record(key, ticket_text, declared, catalog, laya=None, qwen=None):
    """What the ladder WOULD have done (flag off, shadow on). Pure apart from the model calls."""
    t0 = time.time()
    d = infer(inference_text(ticket_text), catalog, laya=laya, qwen=qwen)
    decl = catalog.resolve_ref(declared) if declared else None
    rec = {"ts": round(t0), "key": key, "declared": declared,
           "declared_repo": getattr(decl, "path", None), **d.summary(),
           "agrees": (getattr(decl, "path", None) == getattr(d.repo, "path", None)) if decl and d.repo else None,
           "ms": round(1000 * (time.time() - t0))}
    return rec


def shadow_async(key, ticket_text, declared, get_catalog):
    """Fire-and-forget: log the would-be inference; never touches the spawn path's outcome or timing."""
    def run():
        try:
            rec = shadow_record(key, ticket_text, declared, get_catalog())
        except Exception as e:
            rec = {"ts": round(time.time()), "key": key, "declared": declared, "error": f"{type(e).__name__}: {e}"[:200]}
        log.info("REPO-INFER-SHADOW %s", json.dumps(rec))
        try:
            os.makedirs(os.path.dirname(SHADOW_LOG), exist_ok=True)
            with open(SHADOW_LOG, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError as e:
            log.warning("repo infer shadow log write failed: %s", e)
    t = threading.Thread(target=run, name=f"repo-infer-shadow-{key}", daemon=True)
    t.start()
    return t
