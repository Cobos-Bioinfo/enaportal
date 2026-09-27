#!/usr/bin/env bash
# Keep one open issue per label for a problem the scheduled schema-drift
# workflow finds, so a lasting problem is reported once rather than every week.
#
#   sync_issue.sh open LABEL TITLE REPORT   open the issue, or update it if REPORT changed
#   sync_issue.sh close LABEL MESSAGE       close the open issue, if there is one
#
# Runs in GitHub Actions: needs GH_TOKEN, and the GITHUB_* variables to link the run.
set -euo pipefail

usage="usage: $0 open LABEL TITLE REPORT | close LABEL MESSAGE"
action=${1:?$usage}
label=${2:?$usage}
run="$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"
issue=$(gh issue list --label "$label" --state open --limit 1 --json number --jq '.[0].number // empty')

case $action in
  open)
    title=${3:?$usage}
    report=${4:?$usage}
    # Editing a body notifies nobody, so the digest is what tells a changed
    # report, worth a comment, from the same one found again.
    digest=$(sha256sum "$report" | cut -c1-16)
    body=$(mktemp)
    trap 'rm -f "$body"' EXIT
    {
      cat "$report"
      printf '\nLast changed in %s.\n<!-- report %s -->\n' "$run" "$digest"
    } >"$body"
    if [[ -z $issue ]]; then
      gh label create "$label" --color B60205 --force \
        --description "Found by the scheduled schema-drift workflow"
      gh issue create --title "$title" --label "$label" --body-file "$body"
    elif [[ $(gh issue view "$issue" --json body --jq .body) != *"<!-- report $digest -->"* ]]; then
      gh issue edit "$issue" --body-file "$body"
      gh issue comment "$issue" --body "The report changed in $run. The description is up to date."
    else
      echo "Issue #$issue already reports this."
    fi
    ;;
  close)
    message=${3:?$usage}
    if [[ -n $issue ]]; then
      gh issue close "$issue" --comment "$message Checked in $run."
    fi
    ;;
  *)
    echo "$usage" >&2
    exit 2
    ;;
esac
