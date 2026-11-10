
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


def main():
    candidate = os.environ["CANDIDATE_BRANCH"]

    issues = gh_api(
        f"repos/{REPO}/issues"
        f"?state=closed&labels={LABEL}&per_page=100"
    )

    selected = {}

    for issue in issues:
        if "pull_request" not in issue:
            continue

        pr = gh_api(f"repos/{REPO}/pulls/{issue['number']}")

        if not pr["merged_at"] or pr["base"]["ref"] != "staging":
            continue

        branch = pr["head"]["ref"]
        if branch not in selected or pr["number"] > selected[branch]["number"]:
            selected[branch] = {
                "number": pr["number"],
                "branch": branch,
            }

    run("git", "fetch", "origin", candidate)
    run("git", "switch", "-C", candidate, "FETCH_HEAD")

    succeeded = []
    failed = []

    for item in sorted(selected.values(), key=lambda x: x["number"]):
        branch = item["branch"]
        number = item["number"]

        print(f"Carrying over PR #{number}: {branch}", flush=True)

        fetch = run(
            "git", "fetch", "origin",
            f"refs/heads/{branch}:refs/remotes/origin/{branch}",
            check=False,
        )

        if fetch.returncode:
            failed.append((number, branch, "Source branch unavailable"))
            continue

        merge = run(
            "git", "merge", "--no-ff", "--no-edit",
            f"origin/{branch}",
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

        succeeded.append((number, branch))

    # Publish only after all merge attempts have completed.
    if succeeded:
        run("git", "push", "origin", f"HEAD:refs/heads/{candidate}")

    summary = [
        "## Automated UAT carryover",
        "",
        f"- Candidate: `{candidate}`",
        f"- Successful carryovers: {len(succeeded)}",
        f"- Carryovers requiring developer attention: {len(failed)}",
        "",
    ]

    for number, branch in succeeded:
        summary.append(f"- SUCCESS: PR #{number} (`{branch}`)")

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

    # Failed feature merges are reported, not treated as fatal.
    return 0


if __name__ == "__main__":
    sys.exit(main())
