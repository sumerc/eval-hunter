#!/usr/bin/env python3
"""eval-hunter: mine hard, human-missed bugs from a repo's history, check them for
training-data leakage, and measure which model/effort finds them in a plain PR review.

  hunt.py mine   <repo> [--top 5] [--since YYYY-MM-DD] ...   -> new run dir + candidates.md
  hunt.py add    <run_dir> --fix <sha> [--origin <sha>]       (a bug a human already knows)
  hunt.py contam <run_dir> --ids c1,c3 [--probe claude:claude-opus-5-5]
  hunt.py review <run_dir> --ids c1,c3 [--agents "claude:claude-opus-5-5:medium ..."] [--repeats 3]
  hunt.py grade  <run_dir> [--judge claude-sonnet-5-5]
  hunt.py report <run_dir>

Stdlib only. Every model call is a fresh headless CLI process.
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
# Clones of remote repos, and one directory per run. Both can be moved with env vars.
CACHE = os.path.expanduser(os.environ.get("EVAL_HUNTER_CHECKOUTS", "~/.cache/eval-hunter/checkouts"))
RUNS = os.path.expanduser(os.environ.get("EVAL_HUNTER_RUNS", "~/.cache/eval-hunter/runs"))

DEFAULT_AGENTS = "claude:claude-opus-5-5:medium claude:claude-opus-5-5:high"
DEFAULT_JUDGE = "claude-sonnet-5-5"
# Training cutoffs we know. A fix dated after the cutoff cannot be in the weights.
CUTOFFS = {"claude-opus-5-5": "2026-06", "claude-fable-5-1": "2026-06"}

FIX_RE = re.compile(r"\b(fix\w*|bug\w*|crash\w*|leak\w*|segfault|sigsegv|race|deadlock|null|overflow|"
                    r"corrupt\w*|wrong|incorrect|regression|use-after-free|uaf|dangling)\b", re.I)
SKIP_RE = re.compile(r"\b(docs?|readme|typo|ci|bump|version|changelog|lint|format\w*|wheel|"
                     r"sdist|workflow|deps?|dependabot|revert|warnings?|compil\w*|build|header|message)\b", re.I)
CODE_RE = re.compile(r"\.(c|h|cc|cpp|hpp|py|pyx|go|rs|js|jsx|ts|tsx|java|kt|rb|php|cs|swift|scala)$")
TEST_RE = re.compile(r"(^|/)(tests?|spec|__tests__|testdata)/|_test\.|(^|/)test_|\.spec\.")
HARD_RE = re.compile(r"\brace\b|use-after-free|\buaf\b|dangling|segfault|sigsegv|deadlock|double free|"
                     r"concurren|freed", re.I)
MID_RE = re.compile(r"leak|overflow|null|refcount|decref|incref|corrupt|truncat", re.I)

REVIEW_PROMPT = """You are a code review agent. Review the pull request below. The repository is checked out at this exact commit in the current directory; read any file you need for context.

Find as many defects as possible: bugs, memory or reference-count leaks, error-handling mistakes, logic errors, races, and incomplete changes. For each defect give the file, the function, and a one-sentence explanation of the failure. Order by severity. Do not list style issues.

<diff>
{diff}
</diff>
"""

PROBE_PROMPT = """Answer only from memory. Do not guess; write UNKNOWN for anything you do not actually remember.

Repository: {repo}
1. What is your training knowledge cutoff?
2. {pr_q}
3. Name any bug that this change introduced and that was fixed later: file, function, mechanism, and the fix.
{extra}"""

JUDGE_PROMPT = """You grade one code review against one known bug.

KNOWN BUG (ground truth). It was introduced by the reviewed change and fixed later by this commit:
<fix>
{fix}
</fix>

REVIEW TO GRADE:
<review>
{review}
</review>

Verdict rules:
- "found": the review reports this defect — same code location and the same failure mechanism. Different wording is fine.
- "partial": the review points at the same code but gives a different or insufficient mechanism, or only a symptom.
- "missed": neither.
Quote the review's sentence that matches (empty if missed). "severity" is the severity the review gave that finding (empty if missed)."""

JUDGE_SCHEMA = {"type": "object", "required": ["verdict", "quote", "severity"], "properties": {
    "verdict": {"type": "string", "enum": ["found", "partial", "missed"]},
    "quote": {"type": "string"}, "severity": {"type": "string"}}}

RECALL_PROMPT = """A model was asked, from memory only, about a repository's history. Decide whether its answer shows it already knows the bug below.

KNOWN BUG FIX:
<fix>
{fix}
</fix>

QUESTION THE MODEL WAS ASKED (anything in it was given, not remembered):
<question>
{question}
</question>

MODEL ANSWER:
<answer>
{answer}
</answer>

"yes" = the answer names this bug's mechanism and location with detail that the question did not give; "partial" = vague but pointing the right way; "no" = UNKNOWN, wrong, or only restates the question."""

RECALL_SCHEMA = {"type": "object", "required": ["recalled", "evidence"], "properties": {
    "recalled": {"type": "string", "enum": ["yes", "partial", "no"]}, "evidence": {"type": "string"}}}


# ---------------------------------------------------------------- git helpers
def sh(cmd, cwd=None, inp=None, check=False, timeout=None):
    r = subprocess.run(cmd, cwd=cwd, input=inp, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        sys.exit(f"failed: {' '.join(cmd)}\n{r.stderr}")
    return r.stdout


class Repo:
    def __init__(self, path):
        self.path = path

    def git(self, *a):
        return sh(["git", *a], cwd=self.path)

    def rev(self, ref):
        return self.git("rev-parse", ref).strip()

    def is_anc(self, a, b):
        return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=self.path).returncode == 0

    def date(self, c):
        return self.git("log", "-1", "--format=%as", c).strip()

    def subject(self, c):
        return self.git("log", "-1", "--format=%s", c).strip()


def open_repo(spec):
    """owner/repo, URL, git@ URL or local path -> Repo with blobs available."""
    if os.path.isdir(spec):
        return Repo(os.path.abspath(spec)), os.path.basename(os.path.abspath(spec))
    m = re.match(r"^(?:https?://|git@)?([^/:]+\.[^/:]+)[/:](.+?)(?:\.git)?/?$", spec)
    host, path = (m.group(1), m.group(2)) if m else ("github.com", spec.strip("/"))
    # keep ssh URLs as given: internal forges usually authenticate over ssh
    url = spec if spec.startswith(("git@", "ssh://")) else f"https://{host}/{path}.git"
    dest = os.path.join(CACHE, host, path)
    if not os.path.isdir(dest):
        sh(["git", "clone", "-q", "--filter=blob:none", url, dest], check=True)
    else:
        sh(["git", "-C", dest, "fetch", "-q"])
        sh(["git", "-C", dest, "merge", "-q", "--ff-only", "@{u}"])
    # blame and -S need blobs; a partial clone fetches them one by one and can trip over its
    # own commit-graph, so the miner turns the cache into a full clone once.
    if sh(["git", "-C", dest, "config", "remote.origin.partialclonefilter"]).strip():
        sh(["git", "-C", dest, "config", "--unset", "remote.origin.partialclonefilter"])
        sh(["git", "-C", dest, "fetch", "-q", "--refetch", "origin"], check=True)
    return Repo(dest), path.split("/")[-1]


# ---------------------------------------------------------------- mining
class History:
    """One pass over the history: commit metadata, numstat, and which merge brought each commit in.
    git process spawns dominate the cost, so everything per-commit is read up front."""

    def __init__(self, repo, tip, scan, since):
        self.repo, self.meta, self.stat = repo, {}, {}
        out = repo.git("log", "--numstat", "--format=%x01%H%x00%P%x00%as%x00%s", f"-n{scan}",
                       *([f"--since={since}"] if since else []), tip)
        for rec in out.split("\x01")[1:]:
            head, _, body = rec.partition("\n")
            sha, parents, date, subj = head.split("\x00")
            self.meta[sha] = {"parents": parents.split(), "date": date, "subject": subj}
            self.stat[sha] = [(int(x), int(y), f) for x, y, f in re.findall(r"^(\d+)\t(\d+)\t(.+)$", body, re.M)]
        self.order = list(self.meta)  # newest first
        self.owner = {}  # commit -> first-parent merge that brought it in
        for m in repo.git("rev-list", "--first-parent", "--merges", tip).split():
            for c in repo.git("rev-list", f"{m}^1..{m}^2").split():
                self.owner.setdefault(c, m)
        self._pr = {}

    def info(self, c):
        if c not in self.meta:
            d = self.repo.git("log", "-1", "--format=%P%x00%as%x00%s", c).strip().split("\x00")
            self.meta[c] = {"parents": d[0].split(), "date": d[1], "subject": d[2]}
        return self.meta[c]

    def pr(self, c):
        """The change that brought c into the default branch, diffed from its true merge-base."""
        m = self.owner.get(c)
        key = m or c
        if key in self._pr:
            return self._pr[key]
        if m:
            subj = self.info(m)["subject"]
            num = re.search(r"pull request #(\d+)|merge request \S*!(\d+)|!(\d+)", subj)
            head = self.info(m)["parents"][1]
            pr = {"kind": "merge", "number": num and next(g for g in num.groups() if g), "title": subj,
                  "merge": m, "base": self.repo.git("merge-base", f"{m}^1", head).strip(), "head": head,
                  "merged": self.info(m)["date"]}
        else:
            subj = self.info(c)["subject"]
            num = re.search(r"\((?:#|!)(\d+)\)\s*$", subj)  # GitHub (#12) or GitLab (!12) squash
            pr = {"kind": "squash" if num else "direct", "number": num and num.group(1), "title": subj,
                  "merge": c, "base": self.info(c)["parents"][0], "head": c, "merged": self.info(c)["date"]}
        pr["lines"] = len(self.repo.git("diff", pr["base"], pr["head"]).splitlines())
        self._pr[key] = pr
        return pr


def is_code(f):
    return bool(CODE_RE.search(f)) and not TEST_RE.search(f)


def blame_origin(repo, diff, parent):
    """Commit that introduced the code a fix corrects, read at the fix's parent.
    Changed lines: the most-blamed commit. Pure additions have no old lines; a fix that adds a
    release or a guard often restores something an earlier commit removed, so first look for the
    newest commit that removed lines mentioning the same expressions, then fall back to blaming
    the neighbours (low confidence)."""
    votes, pure, added = {}, True, []
    for fdiff in re.split(r"^diff --git ", diff, flags=re.M)[1:]:
        f = re.search(r"^--- a/(.+)$", fdiff, re.M)
        if not f or not is_code(f.group(1)):
            continue
        path = f.group(1)
        for m in re.finditer(r"^@@ -(\d+)(?:,(\d+))? .*?\n((?:[+-].*\n?)*)", fdiff, re.M):
            start, cnt = int(m.group(1)), int(m.group(2) or 1)
            removed = [l[1:].strip() for l in m.group(3).splitlines() if l.startswith("-")]
            if removed and all(not l or l.startswith(("//", "#", "/*", "*")) for l in removed):
                cnt = 0  # only comments changed: old lines say nothing about the bug
            pure = pure and cnt == 0
            added += [(path, l[1:]) for l in m.group(3).splitlines() if l.startswith("+")]
            lo, hi = (max(1, start - 2), start + 3) if cnt == 0 else (start, start + cnt - 1)
            out = repo.git("blame", "-w", "-M", "-C", "--porcelain", f"-L{lo},{hi}", parent, "--", path)
            for sha in re.findall(r"^([0-9a-f]{40}) \d+ \d+", out, re.M):
                votes[sha] = votes.get(sha, 0) + 1
    if pure:
        removed = removal_origin(repo, added, parent)
        if removed:
            return removed, "medium"
    return (max(votes, key=votes.get) if votes else None), ("low" if pure else "high")


CALL_STMT = r"^\s*\w+\(\s*({expr})\s*\);\s*$"


def removal_origin(repo, added, parent):
    """Newest commit before `parent` that removed a one-call statement on the same expression the
    fix adds back, e.g. the fix adds `Py_XDECREF(pit->name);` and an earlier commit removed
    `Py_CLEAR(pit->name);`. Only `call(expr);` lines count: release, free, close, unlock, decref."""
    best = None
    for path, line in added:
        m = re.match(CALL_STMT.format(expr=r"[\w>.-]+"), line)
        if not m:
            continue
        expr = m.group(1)
        shape = re.compile(CALL_STMT.format(expr=re.escape(expr)))
        for c in repo.git("log", "--follow", "--format=%H %ct", f"-S{expr}", parent, "--", path).splitlines()[:40]:
            sha, ts = c.split()
            patch = repo.git("show", "--format=", sha).splitlines()  # whole commit: path may be pre-rename
            minus = sum(bool(shape.match(l[1:])) for l in patch if l.startswith("-") and not l.startswith("---"))
            plus = sum(bool(shape.match(l[1:])) for l in patch if l.startswith("+") and not l.startswith("+++"))
            if minus > plus:
                if not best or int(ts) > best[1]:
                    best = (sha, int(ts))
                break
    return best and best[0]


def bug_class(text):
    return "hard" if HARD_RE.search(text) else "mid" if MID_RE.search(text) else "plain"


def fix_units(h, max_fix_lines):
    """Fix-like changes, newest first: single commits, plus fix-named merges whose own commits
    are not fix-named (e.g. a "fix-clear-stats-segfault" branch whose commit says "update docs")."""
    def fixy(c):
        subj = h.info(c)["subject"]
        return FIX_RE.search(subj) and not SKIP_RE.search(subj)

    covered = {h.owner.get(c) for c in h.order if len(h.info(c)["parents"]) == 1 and fixy(c)}
    for c in h.order:
        parents = h.info(c)["parents"]
        if not fixy(c):
            continue
        if len(parents) == 1:
            size = sum(a + d for a, d, f in h.stat[c] if is_code(f))
        elif c not in h.owner and c not in covered:  # first-parent merge with no fix commit inside
            stat = h.repo.git("diff", "--numstat", parents[0], c)
            size = sum(int(a) + int(d) for a, d, f in re.findall(r"^(\d+)\t(\d+)\t(.+)$", stat, re.M) if is_code(f))
        else:
            continue
        if 0 < size <= max_fix_lines:
            yield c, parents[0], size


def make_candidate(repo, h, fix, parent, size, origin=None):
    """Everything about one fix; None when it does not qualify as missed-by-humans."""
    diff = repo.git("diff", "-U0", parent, fix)
    conf = "given"
    if not origin:
        origin, conf = blame_origin(repo, diff, parent)
    if not origin or not h.info(origin)["parents"]:
        return None  # nothing to blame, or the root commit
    pr = h.pr(origin)
    if h.owner.get(fix, fix) == pr["merge"]:
        return None  # fixed inside the same change before it merged
    fix_date = h.info(fix)["date"]
    latency = (dt.date.fromisoformat(fix_date) - dt.date.fromisoformat(pr["merged"])).days
    subj = h.info(fix)["subject"]
    body = repo.git("log", "-1", "--format=%b", fix).strip()
    full = repo.git("diff", parent, fix)
    code_diff = "".join("diff --git " + d for d in re.split(r"^diff --git ", full, flags=re.M)[1:]
                        if is_code(d.split()[0][2:]))
    changed = "\n".join(l for l in code_diff.splitlines() if l[:1] in "+-" and l[:3] not in ("+++", "---"))
    cls = bug_class(subj + " " + body + " " + changed)
    score = (math.log2(1 + max(latency, 0)) + 1.5 * math.log2(1 + pr["lines"] / 100)
             + {"hard": 3, "mid": 1, "plain": -2}[cls] - (1 if size > 30 else 0) - (2 if conf == "low" else 0))
    return {"fix": fix, "fix_date": fix_date, "fix_subject": subj, "fix_body": body,
            "fix_diff": "\n".join(code_diff.splitlines()[:150]), "fix_lines": size,
            "origin": origin, "origin_confidence": conf, "origin_date": h.info(origin)["date"],
            "origin_subject": h.info(origin)["subject"], "pr": pr, "pr_lines": pr["lines"],
            "latency_days": latency, "class": cls,
            "refs": sorted(set(re.findall(r"#(\d+)", subj + " " + body))),
            "tests_added": sorted(set(re.findall(r"^\+\s*(?:async\s+)?def (test_\w+)", full, re.M))),
            "score": round(score, 2)}


def tip_of(repo):
    return "origin/HEAD" if repo.git("rev-parse", "--verify", "-q", "origin/HEAD").strip() else "HEAD"


def mine(a):
    repo, name = open_repo(a.repo)
    h = History(repo, tip_of(repo), a.scan, a.since)
    cands = []
    for fix, parent, size in fix_units(h, a.max_fix_lines):
        c = make_candidate(repo, h, fix, parent, size)
        if not c or c["latency_days"] < a.min_latency_days or c["pr_lines"] > a.max_pr_lines:
            continue
        cands.append(c)
        print(f"  {fix[:7]} {c['fix_subject'][:60]:60} <- {c['pr']['kind']} "
              f"{c['pr']['number'] or c['pr']['head'][:7]} [{c['class']}, origin {c['origin_confidence']}]",
              file=sys.stderr)
    tier = {"hard": 2, "mid": 1, "plain": 0}
    rank = lambda c: (c["origin_confidence"] != "low", tier[c["class"]], c["score"])
    ranked = sorted(cands, key=rank, reverse=True)
    top = sorted(ranked[:a.top], key=lambda c: c["fix_date"], reverse=True)
    for i, c in enumerate(top, 1):
        c["id"] = f"c{i}"
    run = os.path.join(RUNS, f"{time.strftime('%Y%m%d-%H%M')}-{name}")
    meta = {"repo": a.repo, "repo_path": repo.path, "name": name, "internal": is_internal(a.repo),
            "scanned": len(h.order), "found": len(cands), "params": vars(a)}
    dump(run, "meta.json", meta)
    dump(run, "candidates.json", top)
    dump(run, "all-candidates.json", ranked)
    write(run, "candidates.md", render_candidates(meta, top))
    print(run)


def add(a):
    """Add a bug a human already knows about (a fix commit or a fix-PR merge) to a run."""
    run = a.run_dir
    meta, cands = load(run, "meta.json"), load(run, "candidates.json")
    repo = Repo(meta["repo_path"])
    h = History(repo, tip_of(repo), meta["params"]["scan"], None)
    fix = repo.rev(a.fix)
    parent = h.info(fix)["parents"][0]
    size = sum(int(x) + int(y) for x, y, f in re.findall(r"^(\d+)\t(\d+)\t(.+)$",
               repo.git("diff", "--numstat", parent, fix), re.M) if is_code(f))
    c = make_candidate(repo, h, fix, parent, size, origin=a.origin and repo.rev(a.origin))
    if not c:
        sys.exit("no usable origin: pass --origin <commit that introduced the bug>")
    c["id"] = f"c{max([int(x['id'][1:]) for x in cands] or [0]) + 1}"
    cands.append(c)
    dump(run, "candidates.json", cands)
    write(run, "candidates.md", render_candidates(meta, cands))
    print(f"{c['id']}: {c['fix_subject'][:60]} <- {c['pr']['kind']} {c['pr']['number'] or c['pr']['head'][:7]} "
          f"(origin {c['origin'][:7]}, {c['origin_confidence']})")


def is_internal(spec):
    """Public forges can be in training data; anything else (a self-hosted forge, ...) cannot.
    A local path is judged by its origin remote."""
    if os.path.isdir(spec):
        spec = sh(["git", "-C", spec, "remote", "get-url", "origin"]).strip() or "local.only/x"
    return not re.search(r"(^|[/@.])(github\.com|gitlab\.com|bitbucket\.org|codeberg\.org)[/:]", spec) \
        and (os.path.isdir(spec) or "." in spec.split("/")[0])


def render_candidates(meta, cands):
    out = [f"# eval-hunter candidates — {meta['name']}", "",
           f"Scanned {meta['scanned']} commits, {meta['found']} qualified, top {len(cands)} by hardness "
           f"score, newest fix first.", ""]
    for c in cands:
        pr = c["pr"]
        label = f"PR/MR {pr['number']}" if pr["number"] else f"commit {pr['head'][:7]} ({pr['kind']})"
        out += [f"## {c['id']} — {c['fix_subject']}", "",
                f"- **fix** `{c['fix'][:7]}` {c['fix_date']}, {c['fix_lines']} lines, class `{c['class']}`, "
                f"score {c['score']}",
                f"- **introduced** by `{c['origin'][:7]}` {c['origin_date']} \"{c['origin_subject'][:70]}\" "
                f"(origin confidence: {c.get('origin_confidence', '?')})",
                f"- **reviewed change** {label}: base `{pr['base'][:7]}` head `{pr['head'][:7]}`, "
                f"merged {pr['merged']}, diff {c['pr_lines']} lines",
                f"- **latent** {c['latency_days']} days" + (f"; refs #{', #'.join(c['refs'])}" if c["refs"] else ""),
                "", "```diff", *c["fix_diff"].splitlines()[:25], "```", ""]
    return "\n".join(out)


# ---------------------------------------------------------------- model calls
def call_claude(model, prompt, cwd, effort=None, tools=None, schema=None, stream=None):
    cmd = ["claude", "-p", "--no-session-persistence", "--model", model, "--permission-mode", "dontAsk"]
    if effort:
        cmd += ["--effort", effort]
    cmd += ["--tools", ""] if tools is None else ["--allowedTools", *tools]
    if schema:
        cmd += ["--output-format", "json", "--json-schema", json.dumps(schema)]
    elif stream:
        cmd += ["--output-format", "stream-json", "--verbose"]
    else:
        cmd += ["--output-format", "json"]
    t0 = time.time()
    r = subprocess.run(cmd, cwd=cwd, input=prompt, capture_output=True, text=True)
    out, res = r.stdout, {}
    if stream:
        with open(stream, "w") as f:
            f.write(out)
        for line in out.splitlines():
            if '"type":"result"' in line:
                res = json.loads(line)
    else:
        try:
            res = json.loads(out)
        except json.JSONDecodeError:
            res = {"result": out, "is_error": True, "stderr": r.stderr[-2000:]}
    return {"text": res.get("result", ""), "structured": res.get("structured_output"),
            "cost_usd": res.get("total_cost_usd"), "turns": res.get("num_turns"),
            "secs": round(time.time() - t0), "error": res.get("is_error") or r.returncode != 0}


def call_codex(model, prompt, cwd, effort, out_md):
    t0 = time.time()
    r = subprocess.run(["codex", "exec", "-m", model, "-c", f'model_reasoning_effort="{effort}"',
                        "-c", "mcp_servers={}", "-s", "read-only", "--skip-git-repo-check", "-o", out_md, "-"],
                       cwd=cwd, input=prompt, capture_output=True, text=True)
    with open(out_md + ".log", "w") as f:
        f.write(r.stdout + r.stderr)
    text = open(out_md).read() if os.path.exists(out_md) else ""
    return {"text": text, "cost_usd": None, "turns": None, "secs": round(time.time() - t0),
            "error": r.returncode != 0}


def judge(model, prompt, schema, cwd):
    r = call_claude(model, prompt, cwd, effort="medium", schema=schema)
    if r["structured"]:
        return r["structured"]
    m = re.search(r"\{.*\}", r["text"] or "", re.S)
    return json.loads(m.group(0)) if m else {"error": (r["text"] or "")[:300]}


def ground_truth(c):
    return f"commit {c['fix'][:7]} ({c['fix_date']}): {c['fix_subject']}\n{c['fix_body']}\n\n{c['fix_diff']}"


# ---------------------------------------------------------------- contamination
def contam(a):
    run, cands = a.run_dir, pick(a)
    meta = load(run, "meta.json")
    res = load(run, "contam.json", {})
    if meta.get("internal") and not a.force:
        for c in cands:
            res[c["id"]] = {"internal": {"internal": True}}
        dump(run, "contam.json", res)
        print("internal repo: cannot be in any model's training data, contamination check skipped (--force to run)")
        return
    for c in cands:
        pr = c["pr"]
        pr_q = (f"What did pull request / merge request #{pr['number']} (\"{pr['title'][:80]}\") change, "
                f"and was a bug in it fixed later?" if pr["number"] else
                f"What did the commit \"{pr['title'][:80]}\" change, and was a bug in it fixed later?")
        extra = "".join(f"{i}. What was issue #{r} about?\n" for i, r in enumerate(c["refs"][:3], 4))
        if c["tests_added"]:
            extra += f"{4 + len(c['refs'][:3])}. Do you know the test(s) {', '.join(c['tests_added'][:3])}? What do they check?\n"
        prompt = PROBE_PROMPT.format(repo=meta["repo"], pr_q=pr_q, extra=extra)
        res[c["id"]] = {}
        for spec in a.probe.split():
            cli, model = spec.split(":", 1)
            cutoff = CUTOFFS.get(model)
            post = bool(cutoff) and c["fix_date"][:7] > cutoff
            ans = call_claude(model, prompt, run)["text"] if cli == "claude" else "(codex probe not supported)"
            verdict = judge(a.judge, RECALL_PROMPT.format(fix=ground_truth(c), question=prompt, answer=ans), RECALL_SCHEMA, run)
            res[c["id"]][model] = {"cutoff": cutoff, "fix_after_cutoff": post, "answer": ans, **verdict}
            print(f"{c['id']} {model}: cutoff {cutoff or '?'}, fix after cutoff: {post}, "
                  f"recalled: {verdict.get('recalled')} — {verdict.get('evidence', '')[:120]}")
    dump(run, "contam.json", res)


# ---------------------------------------------------------------- reviews
def pr_key(c):
    return f"pr-{c['pr']['base'][:7]}-{c['pr']['head'][:7]}"


def prepare_tree(repo, pr, key, run):
    """PR head as a plain source tree (no .git, so no future history to peek at) + the review prompt."""
    d = os.path.join(run, "trees", key)
    if not os.path.isdir(d):
        os.makedirs(d)
        arch = subprocess.run(["git", "archive", pr["head"]], cwd=repo.path, capture_output=True)
        subprocess.run(["tar", "-x", "-C", d], input=arch.stdout, check=True)
    header = f"Pull request {pr['number'] or pr['head'][:7]} (merge of {pr['head'][:7]} into {pr['base'][:7]})"
    log = repo.git("log", "--format=- %h %s", f"{pr['base']}..{pr['head']}")
    diff = repo.git("diff", pr["base"], pr["head"])
    prompt = REVIEW_PROMPT.format(diff=f"{header}\n\n{log}\n{diff}")
    write(run, f"prompts/{key}.txt", prompt)
    return d, prompt


def review(a):
    """One set of reviews per reviewed change; candidates from the same PR share them."""
    run, cands = a.run_dir, pick(a)
    repo = Repo(load(run, "meta.json")["repo_path"])
    agents = [t.split(":") for t in a.agents.split()]
    meta = load(run, "meta.json")
    meta["agents"] = list(dict.fromkeys(meta.get("agents", []) + [f"{m}:{e}" for _, m, e in agents]))
    dump(run, "meta.json", meta)
    prs = {}
    for c in cands:
        prs.setdefault(pr_key(c), c["pr"])
    jobs = []
    for key, pr in prs.items():
        tree, prompt = prepare_tree(repo, pr, key, run)
        for cli, model, effort in agents:
            for k in range(1, a.repeats + 1):
                base = os.path.join(run, "reviews", key, f"{model}-{effort}-r{k}")
                if not os.path.exists(base + ".json"):
                    jobs.append((key, cli, model, effort, k, tree, prompt, base))
    print(f"{len(jobs)} review runs for {len(prs)} reviewed change(s), {a.parallel} in parallel", file=sys.stderr)

    def one(j):
        key, cli, model, effort, k, tree, prompt, base = j
        os.makedirs(os.path.dirname(base), exist_ok=True)
        if cli == "claude":
            r = call_claude(model, prompt, tree, effort=effort, tools=["Read", "Grep", "Glob"], stream=base + ".jsonl")
        else:
            r = call_codex(model, prompt, tree, effort, base + ".md")
        write("", base + ".md", r["text"])
        dump("", base + ".json", {"pr": key, "cli": cli, "model": model, "effort": effort, "repeat": k,
                                  **{x: r[x] for x in ("cost_usd", "turns", "secs", "error")}})
        return f"{key} {model}:{effort} r{k}: {r['secs']}s ${r['cost_usd'] or 0:.2f}{' ERROR' if r['error'] else ''}"

    with cf.ThreadPoolExecutor(a.parallel) as ex:
        for line in ex.map(one, jobs):
            print(line, file=sys.stderr)


def reviews_of(run, c):
    """(review base path, review meta) for every finished review of the candidate's change."""
    d = os.path.join(run, "reviews", pr_key(c))
    if not os.path.isdir(d):
        return []
    return [(os.path.join(d, f[:-5]), json.load(open(os.path.join(d, f)))) for f in sorted(os.listdir(d))
            if f.endswith(".json") and ".grade." not in f]


def grade(a):
    """Grade every review of a change against every known bug in that change."""
    run = a.run_dir
    jobs = [(c, base) for c in pick(a) for base, _ in reviews_of(run, c)
            if not os.path.exists(f"{base}.{c['id']}.grade.json")]

    def one(job):
        c, base = job
        text = open(base + ".md").read()
        g = judge(a.judge, JUDGE_PROMPT.format(fix=ground_truth(c), review=text), JUDGE_SCHEMA, run) \
            if text.strip() else {"verdict": "missed", "quote": "", "severity": "", "note": "empty review"}
        dump("", f"{base}.{c['id']}.grade.json", g)
        return f"{c['id']} {os.path.basename(base)}: {g.get('verdict')}"

    with cf.ThreadPoolExecutor(a.parallel) as ex:
        for line in ex.map(one, jobs):
            print(line, file=sys.stderr)


def report(a):
    run = a.run_dir
    cands = load(run, "candidates.json")
    contam_res = load(run, "contam.json", {})
    rows = {}
    for c in cands:
        for base, meta in reviews_of(run, c):
            gpath = f"{base}.{c['id']}.grade.json"
            if os.path.exists(gpath):
                rows.setdefault(c["id"], {}).setdefault(f"{meta['model']}:{meta['effort']}", []).append(
                    meta | json.load(open(gpath)))
    order = load(run, "meta.json").get("agents") or sorted({k for r in rows.values() for k in r})
    out = [f"# eval-hunter report — {load(run, 'meta.json')['name']}", "",
           "Cells: found / runs (+partial). **hard** = some agent finds it ≤1/3 of the time and another ≥2/3.", "",
           "| id | bug | " + " | ".join(order) + " | contamination | verdict |",
           "|---|---|" + "---|" * len(order) + "---|---|"]
    for c in cands:
        if c["id"] not in rows:
            continue
        cells, rates = [], []
        for ag in order:
            rs = rows[c["id"]].get(ag, [])
            fnd = sum(r.get("verdict") == "found" for r in rs)
            par = sum(r.get("verdict") == "partial" for r in rs)
            cells.append(f"{fnd}/{len(rs)}" + (f" (+{par})" if par else "") if rs else "—")
            if rs:
                rates.append(fnd / len(rs))
        hard = bool(rates) and min(rates) <= 1 / 3 and max(rates) >= 2 / 3
        ct = contam_res.get(c["id"], {})
        ctxt = ", ".join("internal repo" if v.get("internal") else
                         f"{m}: {'post-cutoff' if v.get('fix_after_cutoff') else 'recall=' + str(v.get('recalled'))}"
                         for m, v in ct.items()) or "not checked"
        out.append(f"| {c['id']} | {c['fix_subject'][:50]} | " + " | ".join(cells) +
                   f" | {ctxt} | {'**hard**' if hard else 'easy' if rates and min(rates) >= 2 / 3 else 'too hard' if rates and max(rates) <= 1 / 3 else 'mixed'} |")
    out += ["", "## Cost", ""]
    for c in cands:
        for ag, rs in rows.get(c["id"], {}).items():
            costs = [r["cost_usd"] for r in rs if r.get("cost_usd")]
            secs = [r["secs"] for r in rs if r.get("secs")]
            out.append(f"- {c['id']} {ag}: avg ${sum(costs) / len(costs):.2f}, {sum(secs) // len(secs)}s" if costs and secs
                       else f"- {c['id']} {ag}: n/a")
    out += ["", "## Matching quotes", ""]
    for c in cands:
        for ag, rs in rows.get(c["id"], {}).items():
            for r in rs:
                if r.get("verdict") in ("found", "partial"):
                    out.append(f"- {c['id']} {ag} r{r['repeat']} [{r['verdict']}, {r.get('severity') or '?'}]: "
                               f"{(r.get('quote') or '')[:220]}")
    write(run, "report.md", "\n".join(out))
    print(os.path.join(run, "report.md"))


# ---------------------------------------------------------------- io
def pick(a):
    cands = load(a.run_dir, "candidates.json")
    ids = set(a.ids.split(",")) if a.ids else {c["id"] for c in cands}
    return [c for c in cands if c["id"] in ids]


def load(run, name, default=None):
    p = os.path.join(run, name)
    return json.load(open(p)) if os.path.exists(p) else default


def dump(run, name, obj):
    write(run, name, json.dumps(obj, indent=1, default=str))


def write(run, name, text):
    p = os.path.join(run, name) if run else name
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mine")
    m.add_argument("repo")
    m.add_argument("--top", type=int, default=5)
    m.add_argument("--since", default=None, help="only fixes after this date")
    m.add_argument("--scan", type=int, default=3000, help="newest N commits to scan")
    m.add_argument("--max-fix-lines", type=int, default=80, help="larger fixes make fuzzy ground truth")
    m.add_argument("--max-pr-lines", type=int, default=8000, help="skip changes too big for one prompt")
    m.add_argument("--min-latency-days", type=int, default=7, help="fixed at least this long after merge")
    ad = sub.add_parser("add")
    ad.add_argument("run_dir")
    ad.add_argument("--fix", required=True, help="fix commit, or the merge commit of a fix PR")
    ad.add_argument("--origin", default=None, help="commit that introduced the bug (when blame cannot tell)")
    for name in ("contam", "review", "grade", "report"):
        s = sub.add_parser(name)
        s.add_argument("run_dir")
        s.add_argument("--ids", default=None, help="comma-separated candidate ids (default: all)")
        s.add_argument("--judge", default=DEFAULT_JUDGE)
        s.add_argument("--parallel", type=int, default=4)
        if name == "contam":
            s.add_argument("--probe", default="claude:claude-opus-5-5", help="cli:model tokens")
            s.add_argument("--force", action="store_true", help="probe even an internal repo")
        if name == "review":
            s.add_argument("--agents", default=DEFAULT_AGENTS, help="cli:model:effort tokens")
            s.add_argument("--repeats", type=int, default=3)
    a = p.parse_args()
    {"mine": mine, "add": add, "contam": contam, "review": review, "grade": grade, "report": report}[a.cmd](a)


if __name__ == "__main__":
    main()
