
#!/usr/bin/env python3
"""Read-only staging reset readiness report."""

import json
import os
import subprocess
import sys


def run(*command):
    return subprocess.check_output(command, text=True).strip()


def git(*args):
    return run("git", *args)


def github(*args):
    return json.loads(run("gh", *args))

def is_ancestor(commit, branch):
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, branch],
        capture_output=True,
    )

    if result.returncode not in (0, 1):
        raise RuntimeError(f"Ancestry check failed: {commit} -> {branch}")

    return result.returncode == 0


def check_preservation(staging_ref):
    git(
        "fetch", "origin",
        "+refs/heads/feature/*:refs/remotes/origin/feature/*",
        "--prune",
    )

    branches = git(
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/remotes/origin/feature/",
    ).splitlines()

    commits = git(
        "rev-list",
        "--no-merges",
        f"origin/main..{staging_ref}",
    ).splitlines()

    preserved = []
    missing = []

    for commit in commits:
        matching = [
            branch for branch in branches
            if is_ancestor(commit, branch)
        ]

        if matching:
            preserved.append((commit[:7], matching))
        else:
            missing.append(commit[:7])

    return preserved, missing

def check_merge_commits(staging_ref):
    """Find merges with unexpected content or unsupported structure."""

    merges = git(
        "rev-list", "--merges",
        f"origin/main..{staging_ref}",
    ).splitlines()

    clean = []
    review = []

    for commit in merges:
        parents = git(
            "rev-list", "--parents", "-n", "1", commit
        ).split()

        if len(parents) != 3:
            review.append((commit[:7], "Not a two-parent merge"))
            continue

        diff = git(
            "show",
            "--remerge-diff",
            "--format=",
            commit,
        )

        if diff.strip():
            review.append((commit[:7], "Remerge differs from recorded merge"))
        else:
            clean.append(commit[:7])

    return clean, review

def main():
    staging_ref = os.environ.get("STAGING_REF", "origin/staging")
    git("fetch", "origin", "main", "staging")

    main_sha = git("rev-parse", "origin/main")
    staging_sha = git("rev-parse", staging_ref)

    commits = git(
        "log", "--oneline", f"origin/main..{staging_ref}"
    )

    files = git(
        "diff", "--name-status", "origin/main", staging_ref
    )

    repository = os.environ.get("GITHUB_REPOSITORY") or run(
        "gh", "repo", "view",
        "--json", "nameWithOwner",
        "--jq", ".nameWithOwner"
    )

    prs = github(
        "api", f"repos/{repository}/pulls",
        "-f", "state=open",
        "-f", "base=staging",
        "-f", "per_page=100",
        "--method", "GET"
    )

    preserved, missing = check_preservation(staging_ref)

    clean_merges, review_merges = check_merge_commits(staging_ref)

    if missing:
        result = "Blocked - unpreserved commits detected"
    elif review_merges:
        result = "Manual review required - merge differences detected"
    else:
        result = "Manual review required"


    report = [
        "## Staging reset readiness",
        "",
        f"**Result: {result}**",
        "",
        f"- Main: `{main_sha}`",
        f"- Staging: `{staging_sha}`",
        f"- Open staging PRs (first page): {len(prs)}",
        "",
        "### Staging-only commits",
        "",
        "```text",
        commits or "(none)",
        "```",
        "",
        "### File changes that would be removed",
        "",
        "```text",
        files or "(none)",
        "```",
        "",
        "### Feature preservation",
        "",
        f"- Preserved commits: {len(preserved)}",
        f"- Unpreserved commits: {len(missing)}",
        "",
        *[
            f"- `{commit}` preserved on `{', '.join(branches)}`"
            for commit, branches in preserved
        ],
        *[
            f"- **WARNING:** `{commit}` not found on a remote feature branch"
            for commit in missing
        ],
        "",
        "### Merge-content verification",
        "",
        f"- Merges matching reconstructed result: {len(clean_merges)}",
        f"- Merges requiring review: {len(review_merges)}",
        "",
        *[
            f"- `{commit}` reconstructed merge matches"
            for commit in clean_merges
        ],
        *[
            f"- **REVIEW:** `{commit}` - {reason}"
            for commit, reason in review_merges
        ],
        "",
        "### Important",
        "",
        "- Feature commit ancestry was checked against remote feature branches.",
        "- Merge commits were checked against Git's reconstructed merge results.",
        "- Release completion and UAT handover require manual review.",
        "- No branches were modified.",
    ]

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
    except (subprocess.CalledProcessError, ValueError, RuntimeError) as error:
        print(f"Reset check failed: {error}", file=sys.stderr)
        sys.exit(1)
