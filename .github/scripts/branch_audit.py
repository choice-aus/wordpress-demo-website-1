
#!/usr/bin/env python3
"""Read-only GitHub branch cleanup audit."""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

PROTECTED_LABELS = {
    "uat-failed-retest",
    "deferred",
    "rejected",
    "carryover-next-cycle",
}
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

def get_prs_for_base(repository, base):
    """Retrieve PRs targeting a specific branch."""
    prs = []
    page = 1

    while True:
        batch = github(
            "api", f"repos/{repository}/pulls",
            "--method", "GET",
            "-f", "state=all",
            "-f", f"base={base}",
            "-f", "per_page=100",
            "-f", f"page={page}",
        )

        prs.extend(batch)

        if len(batch) < 100:
            break

        page += 1

    return prs

def get_open_prs_for_branch(repository, branch):
    """Retrieve open PRs originating from a specific branch."""
    prs = []
    page = 1

    owner = repository.split("/")[0]

    while True:
        batch = github(
            "api", f"repos/{repository}/pulls",
            "--method", "GET",
            "-f", "state=open",
            "-f", f"head={owner}:{branch}",
            "-f", "per_page=100",
            "-f", f"page={page}",
        )

        prs.extend(batch)

        if len(batch) < 100:
            break

        page += 1

    return prs

def get_open_prs(repository):
    """Retrieve all open PRs with pagination."""
    prs = []
    page = 1

    while True:
        batch = github(
            "api", f"repos/{repository}/pulls",
            "--method", "GET",
            "-f", "state=open",
            "-f", "per_page=100",
            "-f", f"page={page}",
        )

        prs.extend(batch)

        if len(batch) < 100:
            break

        page += 1

    return prs

def get_prs_for_branch(repository, branch):
    """Retrieve PR history for one branch, across all target branches."""
    prs = []
    page = 1
    owner = repository.split("/")[0]

    while True:
        batch = github(
            "api",
            f"repos/{repository}/pulls",
            "--method", "GET",
            "-f", "state=all",
            "-f", f"head={owner}:{branch}",
            "-f", "per_page=100",
            "-f", f"page={page}",
        )

        prs.extend(batch)

        if len(batch) < 100:
            break

        page += 1

    return prs

def get_latest_release(repository):
    """Find the most recently merged release PR into main."""
    page = 1
    latest = None

    while True:
        batch = github(
            "api", f"repos/{repository}/pulls",
            "--method", "GET",
            "-f", "state=closed",
            "-f", "base=main",
            "-f", "sort=updated",
            "-f", "direction=desc",
            "-f", "per_page=100",
            "-f", f"page={page}",
        )

        for pr in batch:
            if not pr["merged_at"]:
                continue

            if not pr["head"]["ref"].startswith("release-"):
                continue

            if not pr["head"]["repo"]:
                continue

            if (
                pr["head"]["repo"]["full_name"]
                != pr["base"]["repo"]["full_name"]
            ):
                continue

            if (
                latest is None
                or pr["merged_at"] > latest["merged_at"]
            ):
                latest = pr

        if len(batch) < 100:
            break

        page += 1

    if latest is None:
        raise RuntimeError(
            "No merged release-* PR into main was found"
        )

    return latest["head"]["ref"]

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

    labels = {
        label["name"].lower()
        for pr in branch_prs
        for label in pr.get("labels", [])
    }

    protected_labels = labels & PROTECTED_LABELS

    if protected_labels:
        return (
            "Keep",
            "Protected PR label: " + ", ".join(
                sorted(protected_labels)
            ),
        )

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

def get_cleanup_candidates(repository, completed_release):
    """
    Return the cleanup assessment for branches associated with a release.

    Each result contains:
      - branch
      - sha
      - status
      - reason
      - commit
    """
    existing_branches = set(get_branches())

    feature_prs = get_prs_for_base(repository, completed_release)

    release_prs = github(
        "api",
        f"repos/{repository}/pulls",
        "--method", "GET",
        "-f", "state=closed",
        "-f", "base=main",
        "-f", f"head={repository.split('/')[0]}:{completed_release}",
        "-f", "per_page=100",
    )

    release_verified = (
        completed_release in existing_branches
        and any(
            pr["merged_at"]
            and pr["head"]["ref"] == completed_release
            and pr["head"]["repo"]
            and pr["head"]["repo"]["full_name"] == repository
            for pr in release_prs
        )
        and is_ancestor(
            f"origin/{completed_release}",
            "origin/main",
        )
    )

    branches = sorted({
        completed_release,
        *(
            pr["head"]["ref"]
            for pr in feature_prs
            if pr["merged_at"]
            and pr["head"]["repo"]
            and pr["head"]["repo"]["full_name"] == repository
        ),
    })

    prs = feature_prs + release_prs + get_open_prs(repository)

    for branch in branches:
        prs.extend(get_prs_for_branch(repository, branch))

    results = []

    for branch in branches:
        if branch not in existing_branches:
            results.append({
                "branch": branch,
                "sha": None,
                "status": "Keep",
                "reason": "Branch already deleted or unavailable",
                "commit": "N/A",
            })
            continue

        status, reason = classify(
            branch, prs, completed_release, release_verified
        )

        results.append({
            "branch": branch,
            "sha": git("rev-parse", f"origin/{branch}"),
            "status": status,
            "reason": reason,
            "commit": git(
                "log", "-1", "--format=%h %s", f"origin/{branch}"
            ),
        })

    return release_verified, results

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--audit-mode",
        choices=["latest", "specific"],
        default="latest",
    )

    parser.add_argument(
        "--completed-release",
        default="",
    )

    args = parser.parse_args()

    if args.audit_mode == "specific" and not args.completed_release:
        parser.error(
            "--completed-release is required when --audit-mode=specific"
        )

    if args.audit_mode != "specific" and args.completed_release:
        parser.error(
            "--completed-release is only valid when --audit-mode=specific"
        )

    repository = run(
        "gh", "repo", "view",
        "--json", "nameWithOwner",
        "--jq", ".nameWithOwner"
    )

    if args.audit_mode == "latest":
        args.completed_release = get_latest_release(repository)
        print(
            f"Latest merged release identified: "
            f"{args.completed_release}"
        )

    release_verified, assessments = get_cleanup_candidates(
        repository,
        args.completed_release,
    )

    rows = [
        (
            item["branch"],
            item["status"],
            item["reason"],
            item["commit"],
        )
        for item in assessments
    ]

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
