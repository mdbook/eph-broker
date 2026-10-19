#!/usr/bin/env python3
"""prompts.py: load a worker system prompt and hash-gate it against approved-prompts.json.

MECHANISM ONLY. The prompt CONTENT needs the owner's sign-off: a prompt file only RUNS if its
sha256 matches the approved digest in approved-prompts.json. A missing entry or a mismatch raises
PromptNotApproved and nothing spawns; changing a prompt means re-rendering it and signing the new
digest.

Value-free: the gate compares hashes; it neither generates nor embeds a prompt.
"""
import hashlib, json, os, logging

log = logging.getLogger("eph-broker.prompts")


class PromptNotApproved(Exception):
    pass


class PromptLoader:
    def __init__(self, prompts_dir, approved_path, domain="ansible"):
        self.prompts_dir = prompts_dir
        self.approved_path = approved_path
        self.domain = domain

    def _read_approved(self):
        try:
            with open(self.approved_path) as f:
                return json.load(f)          # {"<domain>/<class>": "<sha256>"}
        except (OSError, ValueError):
            return {}

    def sha256(self, prompt_class, domain=None):
        d = domain or self.domain
        path = os.path.join(self.prompts_dir, d, f"{prompt_class}.md")
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest(), path

    def load(self, prompt_class, domain=None):
        """Return the prompt text ONLY if its sha256 is the approved (signed-off) digest.

        domain defaults to self.domain (for single-domain callers). Multi-domain
        callers thread the route's domain so both the prompts/<domain>/<class>.md path and the
        <domain>/<class> approved-prompts.json key resolve from that domain, not the frozen one.
        """
        d = domain or self.domain
        digest, path = self.sha256(prompt_class, d)
        approved = self._read_approved().get(f"{d}/{prompt_class}")
        if approved is None:
            raise PromptNotApproved(
                f"{d}/{prompt_class} has no approved sha256 in {self.approved_path} "
                f"(operator sign-off required before this prompt can run). computed={digest}")
        if approved != digest:
            raise PromptNotApproved(
                f"{d}/{prompt_class} sha256 mismatch: file={digest} approved={approved} "
                f"- prompt changed since sign-off; refusing to run")
        with open(path, encoding="utf-8") as f:
            return f.read()
