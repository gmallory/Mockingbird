---
name: respond-to-copilot-review
description: >-
  Triage and respond to GitHub Copilot's automated pull-request review comments.
  Requests a Copilot review if one hasn't run yet and waits for it to finish,
  then fetches the unresolved comments Copilot left on a PR, decides which are
  worth fixing, applies the code changes, and drafts a reply for every thread —
  then pauses and shows you everything before anything is pushed or posted.
  Use this whenever the user mentions Copilot's review, Copilot PR comments, the
  bot's feedback on a pull request, "address/respond to/reply to the Copilot
  review", or wants to handle, resolve, or act on automated reviewer comments on
  a PR — even if they don't say the word "skill". If a PR has Copilot review
  comments and the user wants to deal with them, use this.
---

# Respond to Copilot PR review comments

Triage GitHub Copilot's automated review comments on a pull request and answer each
one: work out which are worth fixing, make the code changes, and draft a reply for
every thread — then **stop and show everything before anything is pushed or posted.**

Copilot's reviewer is often right but not always. The value here is a careful
human-in-the-loop pass, not rubber-stamping the bot. Treat each comment as a claim to
evaluate against the actual code, not an instruction to obey.

## Prerequisites

- `gh` authenticated (`gh auth status`) and run from inside the repo checkout.
- The PR's branch checked out, since fixes are committed to it.

## Workflow

### 1. Identify the PR

If the user named a PR number, use it. Otherwise infer it from the current branch:

```bash
gh pr view --json number,headRefName,url
```

State which PR you're acting on so there's no ambiguity.

### 2. Ensure a Copilot review exists, and wait for it

Run the bundled helper before triaging. It requests a Copilot review only if one
isn't already done or pending, then blocks until Copilot submits — a single call,
don't loop it yourself:

```bash
.claude/skills/respond-to-copilot-review/scripts/request_copilot_review.sh [PR_NUMBER]
```

Last line is a JSON status:

- `{"status":"already_done",...}` — already reviewed, nothing requested. Proceed.
- `{"status":"completed",...}` — was missing/pending; helper requested (if needed)
  and waited. Proceed.
- `{"status":"timeout",...}` — didn't finish within timeout (default 600s, tune
  via `POLL_TIMEOUT`/`POLL_INTERVAL`). Tell the user it's still pending and stop.

### 3. Gather the unresolved Copilot threads

Run the bundled helper. It returns the unresolved Copilot threads as JSON — each with
the `threadId` (for resolving later), the `commentId` (for replying), and the `path`,
`line`, `body`, and `url`:

The path is relative to this skill's own directory, so use the full path from
the repo root (or `cd` into the skill directory first):

```bash
.claude/skills/respond-to-copilot-review/scripts/list_copilot_threads.sh [PR_NUMBER]
```

Copilot's author login differs between GitHub's APIs (`Copilot` on REST inline
comments, `copilot-pull-request-reviewer` in GraphQL); the helper matches `copilot`
case-insensitively so neither form slips through, and it drops already-resolved
threads so you never re-litigate a closed comment. If it returns `[]`, tell the user
there's nothing unresolved from Copilot and stop.

### 4. Triage each comment

For every thread, open the cited code at `path:line`, read enough surrounding context
to understand it, and judge the comment on its merits. Sort into three buckets:

- **Fix** — correct and worth changing. Most Copilot comments land here.
- **Acknowledge, no change** — technically true but intentional, out of scope for
  this PR, or not worth the churn. You'll explain why in the reply.
- **Push back** — wrong, based on a misread, or a false positive. You'll say so,
  with the specific reason.

Judge against the codebase, not the comment's confidence. Copilot sometimes flags a
"bug" the surrounding code already handles, or proposes something that conflicts with
a documented convention (check `CLAUDE.md` / agent specs). When a comment is right,
fix the **root cause**, not just the single line it happened to point at.

### 5. Apply the fixes

Make the edits for every **Fix** item, grouping comments that touch the same code so
the change stays coherent. If the project has tests or a linter, run them and confirm
they pass — a fix that breaks the build is worse than the comment it resolved. Show
the command and its result as evidence rather than asserting success.

### 6. Draft a reply for every thread

One reply per thread, including the ones you are not changing, since an unanswered
comment reads as ignored. Keep each reply short and factual.

Write replies that do not read as machine-generated, because they post under the
user's name. Use a flat, plain tone: no exclamation marks, no praise of the bot
("good catch", "great point"), no dramatic or deferential filler ("you're absolutely
right", "I've gone ahead and..."), and no em-dashes (use periods, commas, or
parentheses). Aim for a terse developer comment, not an upbeat assistant.

- **Fixed:** name the change. *"Added `MultiEdit` to both matchers so those edits hit
  the same hooks."*
- **No change:** give the reason. *"Leaving this. `MultiEdit` is not a tool in this
  harness, so the omission has no effect here."*
- **Push back:** state the misread. *"`uv pip` is allowed on purpose (line 34), so the
  regex is correct as written."*

A wrong comment gets a plain correction. A right one gets a brief note of what changed.
Do not be deferential for its own sake.

### 7. STOP — show the plan, post nothing yet

Pushing commits and posting replies are outward-facing and awkward to undo, so they
need an explicit go-ahead. Present one scannable summary and then wait:

| # | path:line | Verdict | Fix made | Draft reply |
|---|-----------|---------|----------|-------------|
| 1 | `file:18` | Fix | added MultiEdit to matcher | "Added MultiEdit to the matcher so those edits hit the same hooks." |
| 2 | `file:36` | Push back | none | "Intentional. uv pip is allowed on purpose (line 34)." |

Say plainly that the code edits exist locally but **nothing has been pushed or
posted**, and ask whether to proceed. Let the user amend any verdict or reply first.

### 8. Only after explicit approval: push, then reply

Push the commits, then post one reply per thread using its `commentId`:

```bash
git push
gh api --method POST \
  repos/{owner}/{repo}/pulls/{PR}/comments/{COMMENT_ID}/replies \
  -f body='your reply text'
```

Resolving threads is the most final step — ask about it **separately** rather than
assuming, since the user chose a cautious posture. To resolve a thread once they
agree:

```bash
gh api graphql -f query='
  mutation($t:ID!){ resolveReviewThread(input:{threadId:$t}){ thread { isResolved } } }
' -f t='THREAD_ID'
```

## Command reference

| Need | Command |
|------|---------|
| Current branch's PR | `gh pr view --json number,url` |
| Request Copilot review + wait | `.claude/skills/respond-to-copilot-review/scripts/request_copilot_review.sh [PR]` |
| Unresolved Copilot threads (JSON) | `.claude/skills/respond-to-copilot-review/scripts/list_copilot_threads.sh [PR]` |
| Reply to a comment | `gh api --method POST repos/{owner}/{repo}/pulls/{PR}/comments/{COMMENT_ID}/replies -f body='…'` |
| Resolve a thread | `gh api graphql -f query='mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -f t='THREAD_ID'` |
