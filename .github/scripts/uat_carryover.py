
#!/usr/bin/env python3
"""Integrate labelled carryover PRs into a staging candidate."""

import json
import os
import subprocess
import sys


REPO = os.environ["GITHUB_REPOSITORY"]
LABEL = "carryover-next-cycle"


def run(*args, check=True):
    return subprocess.run(
        args, text=True, capture_output=True, check=check
    )


def gh_api(endpoint):
    result = run("gh", "api", endpoint)
    return json.loads(result.stdout)

def gh_api_paginated(endpoint):
    """Retrieve every page of a GitHub API collection."""
    results = []
    page = 1

    while True:
        separator = "&" if "?" in endpoint else "?"
        items = gh_api(
            f"{endpoint}{separator}per_page=100&page={page}"
        )

        results.extend(items)

        if len(items) < 100:
            break

        page += 1

    return results

def selected_carryover_prs():
    """Return the latest qualifying carryover PR for each feature branch."""
    issues = gh_api_paginated(
        f"repos/{REPO}/issues?state=closed&labels={LABEL}"
    )

    selected = {}

    for issue in issues:
        if "pull_request" not in issue:
            continue

        pr = gh_api(f"repos/{REPO}/pulls/{issue['number']}")

        if not pr["merged_at"] or pr["base"]["ref"] != "staging":
            continue

        branch = pr["head"]["ref"]

        commits = gh_api_paginated(
            f"repos/{REPO}/pulls/{pr['number']}/commits"
        )

        if not commits:
            raise RuntimeError(
                f"Cannot determine revision for PR #{pr['number']}"
            )

        tested_sha = commits[-1]["sha"]

        if branch not in selected or pr["number"] > selected[branch]["number"]:
            selected[branch] = {
                "number": pr["number"],
                "branch": branch,
                "tested_sha": tested_sha,
            }

    return selected

def main():
    candidate = os.environ["CANDIDATE_BRANCH"]

    selected = selected_carryover_prs()

    run("git", "fetch", "origin", candidate)
    run("git", "switch", "-C", candidate, "FETCH_HEAD")

    succeeded = []
    failed = []

    for item in sorted(selected.values(), key=lambda x: x["number"]):
        branch = item["branch"]
        number = item["number"]
        tested_sha = item["tested_sha"]

        print(f"Carrying over PR #{number}: {branch}", flush=True)

        fetch = run(
            "git", "fetch", "origin",
            f"refs/heads/{branch}:refs/remotes/origin/{branch}",
            check=False,
        )

        if fetch.returncode:
            failed.append((number, branch, "Source branch unavailable"))
            continue

        # Confirm the recorded PR revision belongs to the source branch.
        ancestry = run(
            "git", "merge-base", "--is-ancestor",
            tested_sha, f"origin/{branch}",
            check=False,
        )

        if ancestry.returncode != 0:
            failed.append((
                number,
                branch,
                "Recorded PR revision is not in source branch history",
            ))
            continue

        # Merge the revision associated with the staging PR,
        # excluding any commits added to the branch afterwards.
        merge = run(
            "git", "merge", "--no-ff", "--no-edit",
            tested_sha,
            check=False,
        )

        if merge.returncode:
            conflicts = run(
                "git", "diff", "--name-only", "--diff-filter=U"
            ).stdout.strip()

            run("git", "merge", "--abort", check=False)

            if not conflicts:
                print(
                    f"Unexpected merge failure for PR #{number}:\n"
                    f"{merge.stdout}\n{merge.stderr}",
                    file=sys.stderr,
                )
                return 1

            failed.append(
                (number, branch, f"Merge conflict: {conflicts}")
            )
            continue

        succeeded.append((number, branch, tested_sha))

    # Publish only after all merge attempts have completed.
    if succeeded:
        run("git", "push", "origin", f"HEAD:refs/heads/{candidate}")

    # Record the exact revisions successfully carried over.
    manifest = {
        "candidate": candidate,
        "carryovers": [
            {
                "pr": number,
                "branch": branch,
                "tested_sha": tested_sha,
            }
            for number, branch, tested_sha in succeeded
        ],
        "failed": [
            {
                "pr": number,
                "branch": branch,
                "reason": reason,
            }
            for number, branch, reason in failed
        ],
    }

    with open("carryover-manifest.json", "w") as output:
        json.dump(manifest, output, indent=2)

    summary = [
        "## Automated UAT carryover",
        "",
        f"- Candidate: `{candidate}`",
        f"- Successful carryovers: {len(succeeded)}",
        f"- Carryovers requiring developer attention: {len(failed)}",
        "",
    ]

    for number, branch, tested_sha in succeeded:
        summary.append(
            f"- SUCCESS: PR #{number} (`{branch}`) "
            f"at `{tested_sha}`"
        )

    for number, branch, reason in failed:
        summary.append(
            f"- ACTION REQUIRED: PR #{number} (`{branch}`): {reason}"
        )

    summary.append("")
    summary.append("**Staging was not modified.**")

    report = "\n".join(summary)
    print(report)

    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as output:
        output.write(report + "\n")

    if failed:
        print(
            f"PARTIAL SUCCESS: {len(succeeded)} carryover(s) succeeded; "
            f"{len(failed)} require manual attention.",
            file=sys.stderr,
        )
    else:
        print("All selected carryovers completed successfully.")

    return 0

if __name__ == "__main__":
    sys.exit(main())
