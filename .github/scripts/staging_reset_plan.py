
#!/usr/bin/env python3
"""Generate a read-only staging reset plan."""

import os
import subprocess

from release_evidence import verify_promoted_pr

from staging_reset_check import (
    git,
    github_paginated,
    check_preservation,
    check_merge_commits,
    staging_feature_branches,
    merged_pull_requests,
    classify_feature_release,
    run,
)

def file_change_summary(main_ref, staging_ref):
    """Summarise changes caused by resetting staging to main."""

    # Direction matters: staging is the current state,
    # main is the proposed replacement.
    output = git(
        "diff", "--name-status",
        staging_ref, main_ref,
    )

    counts = {
        "added": 0,
        "modified": 0,
        "deleted": 0,
        "renamed": 0,
        "other": 0,
    }

    for line in output.splitlines():
        status = line.split("\t", 1)[0]

        if status.startswith("A"):
            counts["added"] += 1
        elif status.startswith("M"):
            counts["modified"] += 1
        elif status.startswith("D"):
            counts["deleted"] += 1
        elif status.startswith("R"):
            counts["renamed"] += 1
        else:
            counts["other"] += 1

    return counts


def line_change_summary(main_ref, staging_ref):
    """Count line additions and deletions caused by the reset."""

    output = git(
        "diff", "--numstat",
        staging_ref, main_ref,
    )

    added = 0
    deleted = 0
    binary_files = 0

    for line in output.splitlines():
        additions, deletions, _ = line.split("\t", 2)

        if additions == "-" or deletions == "-":
            binary_files += 1
            continue

        added += int(additions)
        deleted += int(deletions)

    return {
        "added": added,
        "deleted": deleted,
        "binary_files": binary_files,
    }


def safety_summary(staging_ref):
    """Summarise staging commit preservation and release awareness."""

    preserved, missing = check_preservation(staging_ref)
    clean_merges, review_merges = check_merge_commits(staging_ref)

    repository = run(
        "gh", "repo", "view",
        "--json", "nameWithOwner",
        "--jq", ".nameWithOwner",
    )

    feature_branches = staging_feature_branches(preserved)
    merged_prs = merged_pull_requests(repository)

    release_results = [
        classify_feature_release(repository, branch, merged_prs)
        for branch in feature_branches
    ]

    statuses = {
        "released": 0,
        "not_confirmed_released": 0,
        "review": 0,
    }

    for item in release_results:
        statuses[item["status"]] += 1

    return {
        "preserved": len(preserved),
        "missing": len(missing),
        "clean_merges": len(clean_merges),
        "review_merges": len(review_merges),
        "release_statuses": statuses,
    }

def reset_decision(safety, open_prs):
    """Classify a proposed reset without authorising execution."""

    if open_prs > 0:
        return (
            "Blocked",
            f"{open_prs} open PR(s) target staging. "
            "Review them before resetting.",
        )

    if (
        safety["missing"] > 0
        or safety["review_merges"] > 0
        or safety["release_statuses"]["review"] > 0
    ):
        return (
            "Blocked",
            "Commit preservation, merge history, or release history "
            "requires investigation.",
        )

    if safety["release_statuses"]["not_confirmed_released"] > 0:
        return (
            "Approval required",
            "Unreleased feature branches require an explicit "
            "UAT carryover decision.",
        )

    return (
        "Ready for approval",
        "Technical checks passed. Human approval is still required.",
    )

def open_staging_pr_count():
    """Count all open pull requests targeting staging."""

    repository = run(
        "gh", "repo", "view",
        "--json", "nameWithOwner",
        "--jq", ".nameWithOwner",
    )

    prs = github_paginated(
        f"repos/{repository}/pulls?state=open&base=staging"
    )

    return len(prs)

def staging_feature_commits(repository, merged_prs):
    """Map original feature commits to merged staging PRs."""

    from staging_reset_check import github_paginated

    staging_prs = [
        pr for pr in merged_prs
        if pr.get("base", {}).get("ref") == "staging"
        and pr.get("head", {}).get("ref", "").startswith("feature/")
    ]

    commits_to_prs = {}

    for pr in staging_prs:
        commits = github_paginated(
            f"repos/{repository}/pulls/{pr['number']}/commits"
        )

        for commit in commits:
            commits_to_prs.setdefault(
                commit["sha"], []
            ).append({
                "pr": pr["number"],
                "branch": pr["head"]["ref"],
                "merge_sha": pr.get("merge_commit_sha"),
            })

    return commits_to_prs

def released_feature_commits(repository, merged_prs):
    """Map original feature commits to verified release promotions."""

    from staging_reset_check import (
        feature_release_prs,
        verify_release_chain,
        github_paginated,
    )

    released = {}

    feature_branches = {
        pr["head"]["ref"]
        for pr in merged_prs
        if pr.get("head", {}).get("ref", "").startswith("feature/")
    }

    for branch in sorted(feature_branches):
        for feature_pr in feature_release_prs(branch, merged_prs):
            chain = verify_release_chain(feature_pr, merged_prs)
            if not chain or not verify_promoted_pr(feature_pr):
                continue
            commits = github_paginated(
                f"repos/{repository}/pulls/{feature_pr['number']}/commits"
            )
            # GitHub's PR commit list must describe exactly the reviewed range.
            head = feature_pr["head"]["sha"]
            squash = feature_pr["merge_commit_sha"]
            base = git("merge-base", f"{squash}^", head)
            expected = set(git("rev-list", head, f"^{base}").splitlines())
            if {item["sha"] for item in commits} != expected:
                continue
            for commit in commits:
                released.setdefault(commit["sha"], []).append(chain)

    return released


def classify_staging_commits(staging_ref, main_ref="origin/main"):
    """Classify individual staging-only commits by release evidence."""

    repository = run(
        "gh", "repo", "view",
        "--json", "nameWithOwner",
        "--jq", ".nameWithOwner",
    )

    merged_prs = merged_pull_requests(repository)
    released = released_feature_commits(repository, merged_prs)
    commits = git(
        "rev-list", "--no-merges", staging_ref, f"^{main_ref}"
    ).splitlines()
    from staging_reset_check import is_ancestor
    return {
        commit: (
            "verified" if any(
                is_ancestor(chain["feature_merge_sha"], main_ref)
                and is_ancestor(chain["release_merge_sha"], main_ref)
                for chain in released.get(commit, [])
            ) else "no_release_evidence"
        )
        for commit in commits
    }


def release_verification_summary(staging_ref, main_ref="origin/main"):
    classifications = classify_staging_commits(staging_ref, main_ref)
    return {
        status: list(classifications.values()).count(status)
        for status in ("verified", "history_only", "no_release_evidence")
    }


def main():
    git("fetch", "origin", "main", "staging")

    staging_ref = os.environ.get("STAGING_REF", "origin/staging")

    main_sha = git("rev-parse", "origin/main")
    staging_sha = git("rev-parse", staging_ref)

    changes = file_change_summary(
        main_sha,
        staging_sha,
    )

    lines = line_change_summary(
        main_sha,
        staging_sha,
    )

    backup_branch = f"backup/staging-before-reset-{staging_sha[:12]}"

    safety = safety_summary(staging_sha)
    release_verification = release_verification_summary(
        staging_sha,
        main_sha,
    )
    open_prs = open_staging_pr_count()
    decision, reason = reset_decision(safety, open_prs)

    print("## Proposed staging reset")
    print()
    print(f"**Decision: {decision}**")
    print()
    print(reason)
    print()
    print(f"- Current main: `{main_sha}`")
    print(f"- Current staging: `{staging_sha}`")
    print()
    print("### File changes caused by reset")
    print()
    print(f"- Added: {changes['added']}")
    print(f"- Modified: {changes['modified']}")
    print(f"- Deleted: {changes['deleted']}")
    print(f"- Renamed: {changes['renamed']}")
    print(f"- Other: {changes['other']}")
    print()
    print("### Line changes caused by reset")
    print()
    print(f"- Lines added: +{lines['added']}")
    print(f"- Lines deleted: -{lines['deleted']}")
    print(f"- Binary files: {lines['binary_files']}")
    print()
    print("### Proposed backup")
    print()
    print(f"- Branch: `{backup_branch}`")
    print(f"- Preserves staging commit: `{staging_sha}`")
    print()
    print("### Commit and release safety")
    print()
    print(f"- Preserved commits: {safety['preserved']}")
    print(f"- Unpreserved commits: {safety['missing']}")
    print(f"- Clean merge commits: {safety['clean_merges']}")
    print(f"- Merges requiring review: {safety['review_merges']}")
    print(f"- Released feature branches: {safety['release_statuses']['released']}")
    print(f"- Open staging PRs: {open_prs}")
    print(
        "- Not confirmed released: "
        f"{safety['release_statuses']['not_confirmed_released']}"
    )
    print(
        "- Release history requiring review: "
        f"{safety['release_statuses']['review']}"
    )
    print()
    print("### Independent release verification")
    print()
    print(f"- Release verified: {release_verification['verified']}")
    print(f"- Release history only: {release_verification['history_only']}")
    print(f"- No release evidence: {release_verification['no_release_evidence']}")
    print()
    print("**Read-only plan. No branches were modified.**")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"Reset plan failed: {error}")
