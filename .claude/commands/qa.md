---
description: Senior QA pass — audit for defects, write regression tests, or review the current diff
argument-hint: "[module path | diff | all] (default: diff if the tree is dirty, else all)"
---

Run a senior QA pass using the `qa-engineer` subagent.

Target: **$ARGUMENTS**

Resolve the target first:

- empty → if `git status --porcelain` shows changes, review those; otherwise audit the whole `app/` tree
- `diff` → only what has changed against `git diff HEAD`
- a path (e.g. `app/repo.py`, `app/providers/`) → audit that
- `all` → the whole `app/` tree

Then delegate to the `qa-engineer` subagent with the resolved target.
Spawn one subagent per module when auditing more than one, so each gets
a full context window for its file and that file's callers.

Require of every finding: a file:line, a concrete failure path
(inputs/state → wrong outcome), and the invariant it threatens. Drop
anything that fails that bar rather than reporting it as a maybe.

Report back:

1. **Confirmed defects**, most severe first — each with its failure path.
2. **Suspected**, clearly separated, each with what would confirm it.
3. **Regression tests written**, and the result of running them.
4. **Nothing found in** — modules audited that came back clean, so the
   silence is legible as coverage rather than as an omission.

Run `.venv/Scripts/python -m pytest` at the end and report the real
result, including failures. Do not fix anything beyond what was asked
without saying so.
