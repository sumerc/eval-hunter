# eval-hunter

A Claude Code skill that builds code-review eval items from a repository's own history.

It finds bugs that a pull request (or merge request) brought in, that nobody caught in review, and
that a later commit fixed. Then it gives the same PR diff to a review agent many times, at several
models and effort levels, and measures how often each one finds the real bug.

The fixed bug is the ground truth, so you do not have to write expected answers. Because the bug
comes from real history, the score shows how a reviewer does on a real diff, not on a puzzle.

## What it answers

- Which real bugs in this repo does a reviewer miss at one effort level and find at a higher one?
- How often (found / runs) does each model × effort find each bug?
- Could the bug fix be in the model's training data?

## Requirements

- Python 3.9+ (standard library only)
- git
- [Claude Code](https://claude.com/claude-code) CLI (`claude`), logged in
- Optional: the Codex CLI (`codex`), for `codex:<model>:<effort>` agents

## Install

Link the folder into your Claude Code skills directory:

```
ln -s "$PWD" ~/.claude/skills/eval-hunter
```

Then, in Claude Code:

```
/eval-hunter owner/repo
```

The skill walks you through the steps and asks before it spends money. You can also call the
script directly (see below).

## How it works

```
python3 hunt.py mine   <repo> [--top 5] [--since YYYY-MM-DD]     # candidates -> new run dir
python3 hunt.py add    <run> --fix <sha> [--origin <sha>]         # a bug you already know about
python3 hunt.py contam <run> [--ids c1,c3]                        # training-data check
python3 hunt.py review <run> [--ids ...] [--agents "..."] [--repeats 3] [--parallel 4]
python3 hunt.py grade  <run>                                      # judge each review vs the fix
python3 hunt.py report <run>                                      # report.md
```

1. **mine** reads the history newest first. It looks at fix commits, and at fix-named merges
   whose own commits are not named as fixes. For each fix it finds the commit that brought the
   bug in:
   - if the fix changed existing lines, it blames those lines;
   - if the fix only added lines, it looks for an earlier commit that removed the same
     `call(expr);` statement.
   It then finds the PR that brought that commit in, and diffs the PR from its true merge-base.
   It keeps a fix only if the fix came at least 7 days after that PR merged, and was not part of
   the PR itself. Results are ranked by origin confidence, bug class, how long the bug lived, and
   PR size.
2. **add** registers a bug you already know about. Use `--origin` when blame cannot find where
   the bug came from (for example, a bug caused by a missing guard).
3. **contam** checks whether the fix could be in a model's training data (see below). For
   internal repos it skips this check.
4. **review** runs `candidates × agents × repeats` reviews. Each review gets:
   - a minimal prompt (`REVIEW_PROMPT` in `hunt.py`),
   - the PR's commit list and diff,
   - the PR head as a plain source tree with no `.git`, so the fix cannot leak in,
   - the tools Read, Grep and Glob only.

   Bugs that come from the same PR share one set of reviews.
5. **grade** asks a judge model (Sonnet 5.5 by default) whether each review found the known bug:
   `found`, `partial` or `missed`.
6. **report** writes a table: one row per bug, one column per agent, with `found/runs`.

Agents are `cli:model:effort` tokens, for example
`--agents "claude:claude-opus-5-5:medium claude:claude-opus-5-5:high codex:gpt-6-astra:high"`.

## Files

- Remote repos are cloned to `~/.cache/eval-hunter/checkouts/<host>/<path>`. Set
  `EVAL_HUNTER_CHECKOUTS` to change this.
- Each run gets its own directory under `~/.cache/eval-hunter/runs/<timestamp>-<repo>/`. Set
  `EVAL_HUNTER_RUNS` to change this.

A run directory contains:

```
candidates.md / candidates.json      the shortlist (all-candidates.json: everything mined)
contam.json                          training-data check per candidate
prompts/pr-<base>-<head>.txt         the exact review prompt
trees/pr-<base>-<head>/              PR head source tree, no .git
reviews/pr-<base>-<head>/            <model>-<effort>-r<k>.{md,json,jsonl}, <...>.<cid>.grade.json
report.md
```

Steps can be re-run. Reviews and grades that already exist are not redone.

## Training-data check

- **Internal repos** (not on github.com, gitlab.com, bitbucket.org or codeberg.org) are skipped.
  Their code cannot be in a public model's training data.
- **Public repos** get two checks:
  - the fix date is compared with the model's known cutoff (`CUTOFFS` in `hunt.py`);
  - a recall probe asks the model, with no tools, about the PR and any bug fixed in it later.
    The fix's commit subject is not in the question. A judge then decides whether the answer
    shows real recall.

A clean probe means the model does not *recall* the bug. It does not prove the bug is absent from
the training data.

## Cost

These are rough figures for Opus 5.5 on a PR diff of about 4,000 lines:

| Effort | Cost per review | Time per review |
|---|---|---|
| medium | $0.9 | 1–2 min |
| high | $1.0–1.2 | 2–3 min |
| xhigh | $1.6–1.8 | 5–7 min |
| max | about $6 | up to 25 min |

The skill states the number of runs and an estimate, and waits for your go.

## Privacy

An eval item contains a large part of the reviewed code: the PR diff, the fix, and the source
tree at the PR head. Running the skill sends this code to the model provider, the same as using
Claude Code on that repo. Keep run directories and any eval set built from an internal repo inside
your organisation.

## Example

See [example-output.md](example-output.md).

## Known limits

- **Pure-addition fixes:** when a fix only adds lines, `mine` blames the nearby lines and picks
  the commit with the most votes. That can be an old commit. Pass `--origin` with `add` instead.
- **Strict judge:** the judge sometimes grades `partial` when a review states the same function
  and the same mechanism. Spot-check `partial` grades before you trust the report.
- **When the bug started:** blame names the last change to the buggy lines. Sometimes the flaw is
  older, and that change only reshaped it or made it easier to hit. The tool does not decide
  this; you decide when you pick candidates.
- **Reported severity:** the severity a reviewer gives a finding changes between repeats. Grading
  ignores it.
- **Effort level:** Claude Code does not record the effort level in its output stream. `hunt.py`
  passes `--effort` explicitly and stores it with each review.
