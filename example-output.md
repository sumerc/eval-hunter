# Example output

This is `report.md` from a real run, copied unchanged. A fresh Claude Code session (Opus 5.5) was given this prompt:

> /eval-hunter sumerc/yappi - I want to test the bug fixed in PR #207 (issue #188, segfault when clear_stats runs while a callback is in flight). Review the PR that introduced it with Opus 5.5 at medium, high and xhigh effort, 4 repeats each, and show me the report.

What the session did on its own:
- It traced the origin to commit `6b755c5`, which came in through PR #27 (a 3,914-line diff).
- It ran the training-data probe; the bug was not recalled.
- It ran 12 reviews, at a total cost of $14.90.
- It added the last section after it checked the judge's `partial` grades against the review texts.

---

# eval-hunter report — yappi

Cells: found / runs (+partial). **hard** = some agent finds it ≤1/3 of the time and another ≥2/3.

| id | bug | claude-opus-5-5:medium | claude-opus-5-5:high | claude-opus-5-5:xhigh | contamination | verdict |
|---|---|---|---|---|---|---|
| c1 | Merge pull request #207 from sumerc/fix-clear-stat | 0/4 | 1/4 (+1) | 1/4 (+3) | claude-opus-5-5: recall=no | too hard |

## Cost

- c1 claude-opus-5-5:high: avg $1.02, 162s
- c1 claude-opus-5-5:medium: avg $0.91, 76s
- c1 claude-opus-5-5:xhigh: avg $1.80, 412s

## Matching quotes

- c1 claude-opus-5-5:high r2 [found, Medium]: The Python tag and context-id callbacks run bytecode, so the GIL can switch threads in the middle of a callback. If another thread calls `clear_stats()` during that window, `_call_funcobjargs` restores `current_ctx`/`pre
- c1 claude-opus-5-5:high r4 [partial, Medium]: `_call_funcobjargs`.** The function saves `current_ctx`/`prev_ctx` and restores them blindly after the Python callback. If the callback releases the GIL and another thread runs `clear_stats()`, the restored pointer is fr
- c1 claude-opus-5-5:xhigh r1 [partial, Medium]: The tag callback runs Python on every call event, and the GIL can switch while it runs. If another thread calls `clear_stats()` in that time, the restore puts back a freed `current_ctx`, and `_get_pits_tbl` then uses fre
- c1 claude-opus-5-5:xhigh r2 [found, High]: If another thread runs `clear_stats()` during the callback, `_call_funcobjargs` then restores a freed `current_ctx`, which is a use-after-free.
- c1 claude-opus-5-5:xhigh r3 [partial, Medium]: The tag callback is Python code that now runs on every call event and can release the GIL. If another thread runs `clear_stats()` meanwhile, the saved `current_ctx` is restored as a dangling pointer, and `_get_pits_tbl` 
- c1 claude-opus-5-5:xhigh r4 [partial, Medium]: If another thread then runs `yappi.clear_stats()`, `_call_funcobjargs` restores a freed `current_ctx`, and `_get_pits_tbl` dereferences its destroyed `tags` table. That is a use-after-free.
## Manual grade check (overrides the judge's verdict above)

The judge's `partial` grades for high r4 and xhigh r1, r3, r4 are wrong. Each of them names `_call_funcobjargs`
and gives the same mechanism as the `found` grades: the Python callback lets another thread run `clear_stats()`,
the saved `current_ctx` is then restored as a freed pointer, and `_call_enter`/`_get_pits_tbl` use it (use-after-free).
xhigh r4 #11 and xhigh r2 #6 say almost the same thing, but the judge graded them differently. The `missed`
grades are correct: those reviews mention `clear_stats` only for the `_del_pit` ref leak.

| id | claude-opus-5-5:medium | claude-opus-5-5:high | claude-opus-5-5:xhigh | verdict |
|---|---|---|---|---|
| c1 | 0/4 | 2/4 | 4/4 | **hard** |

Note: running `hunt.py report` again overwrites this section.
