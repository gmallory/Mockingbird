#!/usr/bin/env bash
# List UNRESOLVED GitHub Copilot review threads on a pull request, as JSON.
#
# Usage: list_copilot_threads.sh [PR_NUMBER]
#   PR_NUMBER defaults to the PR for the current branch.
#
# Output: a JSON array. Each element:
#   { threadId, isOutdated, path, line, commentId, url, body }
#     - threadId : GraphQL node id (PRRT_...) — pass to resolveReviewThread
#     - commentId: REST databaseId           — pass to the /replies endpoint
#
# Why a script: every run needs the same GraphQL query, and Copilot's author
# login differs by API surface (`Copilot` on REST inline comments,
# `copilot-pull-request-reviewer` in GraphQL). Anchoring on a leading
# `copilot` (optionally followed by `-...`) catches both without matching
# human logins that merely contain "copilot"; filtering isResolved drops
# closed threads.
set -euo pipefail

command -v gh >/dev/null 2>&1 || { echo "error: gh CLI not found" >&2; exit 1; }

pr="${1:-}"
read -r owner name <<<"$(gh repo view --json owner,name --jq '"\(.owner.login) \(.name)"')"
[ -n "${pr}" ] || pr="$(gh pr view --json number --jq .number)"

gh api graphql \
  -f owner="$owner" -f name="$name" -F pr="$pr" \
  -f query='
    query($owner:String!, $name:String!, $pr:Int!) {
      repository(owner:$owner, name:$name) {
        pullRequest(number:$pr) {
          reviewThreads(first:100) {
            nodes {
              id isResolved isOutdated path
              comments(first:1) {
                nodes { databaseId author { login } body diffHunk url line originalLine }
              }
            }
          }
        }
      }
    }' \
  --jq '
    .data.repository.pullRequest.reviewThreads.nodes
    | map(select(
        (.isResolved | not)
        and ((.comments.nodes[0].author.login // "") | ascii_downcase | test("^copilot(-|$)"))
      ))
    | map({
        threadId:  .id,
        isOutdated: .isOutdated,
        path:      .path,
        line:      (.comments.nodes[0].line // .comments.nodes[0].originalLine),
        commentId: .comments.nodes[0].databaseId,
        url:       .comments.nodes[0].url,
        body:      .comments.nodes[0].body
      })'
