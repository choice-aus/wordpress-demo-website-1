
#!/usr/bin/env python3
"""Read-only GitHub branch cleanup audit."""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone


PROTECTED_LABELS = {"uat-failed-retest", "deferred", "rejected"}
TICKET_PATTERN = re.compile(r"\b[A-Z]{3}-\d+\b")


def run(*command):
    """Execute a command and return its output."""
    result = subprocess.run(
        command, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def git(*args):
    return run("git", *args)


def github(*args):
    return json.loads(run("gh", *args))


def get_branches():
    git("fetch", "origin", "+refs/heads/*:refs/remotes/origin/*", "--prune")
    output = git("branch", "-r", "--format=%(refname:short)")
    return sorted(
        branch.removeprefix("origin/")
        for branch in output.splitlines()
        if branch.startswith(("origin/feature/", "origin/release-"))
    )


def get_prs(repository):
    """Retrieve all PRs, not just the most recent 100."""
    prs = []
    page = 1

    while True:
        batch = github(
            "api", f"repos/{repository}/pulls",
            "--method", "GET",
            "-f", "state=all",
            "-f", "per_page=100",
            "-f", f"page={page}",
        )
        prs.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    return prs


def get_labels(repository, number):
    return {
        label["name"].lower()
        for label in github(
            "api", f"repos/{repository}/issues/{number}/labels"
        )
    }


def is_ancestor(branch, target):
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", branch, target],
        capture_output=True,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"Git ancestry check failed for {branch}")
    return result.returncode == 0


def ticket_ids(text):
    return set(TICKET_PATTERN.findall(text or ""))



def classify(branch, prs, completed_release, release_verified):
    branch_prs = [
        pr for pr in prs
        if pr["head"]["ref"] == branch
        and pr["head"]["repo"]
        and pr["head"]["repo"]["full_name"] == pr["base"]["repo"]["full_name"]
    ]

    if any(pr["state"] == "open" for pr in branch_prs):
        return "Keep", "Open PR exists"

    if branch.startswith("release-"):
        if branch != completed_release:
            return "Keep", "Not the explicitly completed release"

        if not release_verified:
            return "Needs review", "Release merge not verified"

        if not is_ancestor(f"origin/{branch}", "origin/main"):
            return "Needs review", "Release has unmerged changes"

        return "Cleanup candidate", "Release merged into main; closure declared"

    if not release_verified:
        return "Needs review", "No verified completed release"

    current_sha = git("rev-parse", f"origin/{branch}")

    release_prs = [
        pr for pr in branch_prs
        if pr["base"]["ref"] == completed_release
        and pr["merged_at"]
    ]

    if not release_prs:
        return "Needs review", "No direct merged PR into completed release"

    for pr in release_prs:
        reviewed_sha = pr["head"]["sha"]
        squash_sha = pr["merge_commit_sha"]

        if reviewed_sha != current_sha:
            continue

        if not squash_sha:
            continue

        if is_ancestor(squash_sha, "origin/main"):
            return (
                "Cleanup candidate",
                f"PR #{pr['number']} tip verified; squash deployed to main"
            )

    return (
        "Needs review",
        "No release PR matches current tip with deployed squash commit"
    )



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--completed-release", default="")
    args = parser.parse_args()

    repository = run(
        "gh", "repo", "view",
        "--json", "nameWithOwner",
        "--jq", ".nameWithOwner"
    )

    branches = get_branches()
    prs = get_prs(repository)

    release_prs = [
        pr for pr in prs
        if pr["head"]["ref"] == args.completed_release
        and pr["base"]["ref"] == "main"
        and pr["merged_at"]
    ]

    release_verified = bool(args.completed_release and release_prs)

    rows = []
    for branch in branches:
        branch_prs = [
            pr for pr in prs if pr["head"]["ref"] == branch
        ]

        labels = set()
        for pr in branch_prs:
            labels.update(get_labels(repository, pr["number"]))

        status, reason = classify(
            branch, prs, args.completed_release, release_verified
        )

        if labels & PROTECTED_LABELS:
            status = "Keep"
            reason = "Protected PR label: " + ", ".join(
                sorted(labels & PROTECTED_LABELS)
            )

        commit = git("log", "-1", "--format=%h %s", f"origin/{branch}")

        rows.append((branch, status, reason, commit))


    counts = {
        "Cleanup candidate": 0,
        "Needs review": 0,
        "Keep": 0,
    }

    for _, status, _, _ in rows:
        counts[status] += 1

    report = [
        "## Branch cleanup audit",
        "",
        f"Completed release: `{args.completed_release or 'Not supplied'}`",
        f"Release merge verified: {'Yes' if release_verified else 'No'}",
        "",
        "### Summary",
        "",
        f"- Cleanup candidates: **{counts['Cleanup candidate']}**",
        f"- Needs review: **{counts['Needs review']}**",
        f"- Keep: **{counts['Keep']}**",
        "",
        "### Branch details",
        "",
        "| Branch | Classification | Reason | Latest commit |",
        "|---|---|---|---|",
    ]

    for branch, status, reason, commit in rows:
        report.append(
            f"| `{branch}` | {status} | {reason} | {commit} |"
        )

    report.extend([
        "",
        "### Important",
        "",
        "- Cleanup candidates are recommendations, not deletion approvals.",
        "- A merged release is not necessarily a closed release.",
        "- Ambiguous branches require manual review.",
        "",
        "_Read-only audit. No branches were modified._",
    ])

    output = "\n".join(report)
    print(output)

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(
            os.environ["GITHUB_STEP_SUMMARY"],
            "a",
            encoding="utf-8",
        ) as summary:
            summary.write(output + "\n")

if __name__ == "__main__":
    try:
        main()
    except (subprocess.CalledProcessError, RuntimeError) as error:
        print(f"Audit failed: {error}", file=sys.stderr)
        sys.exit(1)
