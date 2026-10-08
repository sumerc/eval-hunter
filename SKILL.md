---
name: eval-hunter
description: >
  Build code-review eval items from a repo's own history. Mines bugs that a PR/MR introduced,
  humans missed, and a later commit fixed; ranks the hardest; checks public repos for
  training-data leakage; then reviews each PR's diff with a minimal prompt across
  model × effort, N repeats each, and grades every review against the real fix. Use when I say
  eval-hunter, /eval-hunter, "hunt eval candidates", or ask which hard bugs in a repo a model
  finds at medium but not high effort. Args: a repo (owner/repo, URL, or path), optional
  agents, efforts, repeats, top N.
---

# eval-hunter

One script, five steps. Every step writes into a run dir under `~/.cache/eval-hunter/runs/`
and can be re-run; reviews and grades that exist are not redone.

```
H=~/.claude/skills/eval-hunter/hunt.py
python3 $H mine   <repo> [--top 5] [--since YYYY-MM-DD]      # prints the run dir
python3 $H add    <run> --fix <sha> [--origin <sha>]          # a bug I already know about
python3 $H contam <run> [--ids c1,c3]
python3 $H review <run> --ids c1,c3 [--agents "..."] [--repeats 3] [--parallel 4]
python3 $H grade  <run>
python3 $H report <run>
```

Run model steps in the background (`run_in_background`); `review` takes minutes per call.
If your Bash tool is sandboxed, the nested `claude` calls need it disabled.

## 1. Mine → human picks

`mine` scans history newest first for fix commits (and fix-named merges whose own commits
are not fix-named). For each fix it finds the commit that introduced the bug: it blames the
lines the fix changed; for pure additions it looks for an earlier commit that removed the same
`call(expr);` statement. Then it finds the PR/MR that brought that commit in and diffs it from
the true merge-base. It keeps the fix only if it came at least 7 days after that PR merged and
was not part of the PR itself. Ranking: trustworthy origin first, then bug class
(race/UAF/segfault > leak/NULL/overflow > other), then latency and PR size. The top N are listed newest
fix first.

Read `candidates.md` and show me a short table: id, fix subject, fix date, PR, PR diff
size, latency, class, origin confidence. Flag anything doubtful (origin `low`, PR diff over
~4000 lines, fix that looks like a build or version-compat change). Then ask me with
AskUserQuestion (multiSelect) which ids to keep. If I name a bug I remember, use `add`
(pass `--origin` when blame cannot tell; `add` says so).

Origin confidence: `high` = blame of changed lines, `medium` = removed-statement search,
`low` = only neighbours blamed (often wrong), `given` = I passed `--origin`.

## 2. Contamination

`contam` decides per candidate whether the fix could be in a model's weights:

- **Internal repo** (not github.com / gitlab.com / bitbucket.org / codeberg.org, or a local path
  whose origin is not public): skipped — it cannot be in training data.
- **Public repo:** fix date vs the model's known cutoff (`CUTOFFS` in `hunt.py`; after the
  cutoff = clean), plus a no-tools recall probe about the PR, the fix commit, its issue refs and
  the tests it added. A judge decides whether the answer reveals the bug.

Show the result per candidate. `recalled: yes` disqualifies the item; `partial` is my call.

## 3. Review

Before starting, state the run count and a cost estimate and wait for my go: runs =
candidates × agents × repeats. Rough cost per Opus 5.5 review: ~$1 at medium/high on a
4k-line diff, ~$6 at max.

Agents are `cli:model:effort` tokens, `cli` = `claude` or `codex`:

- default: `claude:claude-opus-5-5:medium claude:claude-opus-5-5:high`
- example: `--agents "claude:claude-opus-5-5:medium claude:claude-opus-5-5:high claude:claude-opus-5-5:max codex:gpt-6-astra:high"`

Each review sees exactly what a PR reviewer saw: the minimal prompt (`REVIEW_PROMPT` in
`hunt.py`, the one from the yappi experiment), the PR's commit list and its diff from the
merge-base, and the PR head as a plain source tree with **no `.git`**, so no later history
(and no fix) can leak in. Tools: Read, Grep, Glob only, with `--permission-mode dontAsk`.

## 4. Grade and report

`grade` asks a judge model (default Sonnet 5.5) whether each review found the known bug:
`found` (same location and mechanism), `partial` (same code, different or weaker
mechanism, or only a symptom), `missed`. `report` writes `report.md`: one row per candidate,
one column per agent with `found/runs (+partial)`, the contamination status, and a verdict:

- **hard**: some agent finds it in ≤1/3 of runs and another in ≥2/3 — the eval items I want
- **easy**: every agent ≥2/3 — a floor check
- **too hard**: every agent ≤1/3
- **mixed**: anything else

Present the table, then each **hard** item in two lines: what the bug is and which agent
separates. Spot-check one `found` and one `missed` grade against the review text before
trusting the judge.

## Known limits

- A bug introduced by a missing guard (nothing removed, nothing changed) has no blamable
  origin; `mine` cannot place it. Use `add --origin`.
- Blame names the last change that touched the buggy lines. Sometimes the flaw is older and
  that change only reshaped or widened it. `mine` does not try to decide this; I do, when
  picking candidates.
- The effort level is not recorded in Claude Code's stream; `hunt.py` passes `--effort`
  explicitly and stores it in each review's `.json`. Never rely on `settings.json` defaults.
- `claude --bare` skips the keychain login; the script does not use it.
- Severity given by the reviewer is unstable across repeats; the report shows it but grading
  ignores it.
