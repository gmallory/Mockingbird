#!/usr/bin/env bash
# Ensure a GitHub Copilot review exists on a pull request, then wait for it.
#
# Usage: request_copilot_review.sh [PR_NUMBER]
#   PR_NUMBER defaults to the PR for the current branch.
#
# Behaviour:
#   - If Copilot has already submitted a review and is not pending, prints
#     {"status":"already_done",...} and exits 0 (nothing to wait for).
#   - If Copilot is already a pending reviewer, skips the request and polls.
#   - Otherwise requests a Copilot review, then polls until it lands.
#
# Output (last line): a JSON status object, e.g.
#   {"status":"completed","pr":10,"submittedAt":"2026-06-30T00:05:04Z","state":"COMMENTED"}
#   {"status":"already_done","pr":10,...}
#   {"status":"timeout","pr":10,"waited":600}
# Exit code is 0 on already_done/completed, 1 on timeout, 2 on error.
#
# Tunables (env):
#   POLL_INTERVAL  seconds between polls (default 15)
#   POLL_TIMEOUT   max seconds to wait    (default 600)
#
# Copilot's reviewer login differs by API surface (`Copilot` user-facing,
# `copilot-pull-request-reviewer` on submitted reviews), so logins are matched
# on a leading `copilot` to catch both. The request endpoint needs the bot slug
# `copilot-pull-request-reviewer[bot]`.
set -euo pipefail

command -v gh >/dev/null 2>&1 || { echo '{"status":"error","msg":"gh CLI not found"}' >&2; exit 2; }

POLL_INTERVAL="${POLL_INTERVAL:-15}"
POLL_TIMEOUT="${POLL_TIMEOUT:-600}"

pr="${1:-}"
read -r owner name <<<"$(gh repo view --json owner,name --jq '"\(.owner.login) \(.name)"')"
[ -n "${pr}" ] || pr="$(gh pr view --json number --jq .number)"

# Returns the current Copilot state as `HAS_REVIEW PENDING SUBMITTED_AT STATE`.
poll_state() {
  gh pr view "$pr" --json latestReviews,reviewRequests --jq '
    def iscop: (ascii_downcase | test("^copilot(-|$)"));
    (.latestReviews // [] | map(select((.author.login // "") | iscop)) | last) as $r
    | (.reviewRequests // [] | map(select((.login // "") | iscop)) | length > 0) as $pending
    | "\(if $r then "yes" else "no" end) \(if $pending then "yes" else "no" end) \($r.submittedAt // "-") \($r.state // "-")"
  '
}

read -r has_review pending submitted_at state <<<"$(poll_state)"

# Already reviewed and nothing pending: done, no request needed.
if [ "$has_review" = "yes" ] && [ "$pending" = "no" ]; then
  printf '{"status":"already_done","pr":%s,"submittedAt":"%s","state":"%s"}\n' "$pr" "$submitted_at" "$state"
  exit 0
fi

# Not requested and not pending: request a Copilot review.
if [ "$pending" = "no" ]; then
  echo "requesting Copilot review on PR #$pr..." >&2
  gh api --method POST "repos/$owner/$name/pulls/$pr/requested_reviewers" \
    -f 'reviewers[]=copilot-pull-request-reviewer[bot]' >/dev/null
fi

# Poll until Copilot submits a review and is no longer pending, or we time out.
waited=0
while [ "$waited" -lt "$POLL_TIMEOUT" ]; do
  sleep "$POLL_INTERVAL"
  waited=$((waited + POLL_INTERVAL))
  read -r has_review pending submitted_at state <<<"$(poll_state)"
  echo "waited ${waited}s: has_review=$has_review pending=$pending" >&2
  if [ "$has_review" = "yes" ] && [ "$pending" = "no" ]; then
    printf '{"status":"completed","pr":%s,"submittedAt":"%s","state":"%s","waited":%s}\n' "$pr" "$submitted_at" "$state" "$waited"
    exit 0
  fi
done

printf '{"status":"timeout","pr":%s,"waited":%s}\n' "$pr" "$waited"
exit 1
