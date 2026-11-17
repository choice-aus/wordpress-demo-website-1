
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

def github_paginated(endpoint):
    """Retrieve all pages of results from a GitHub API endpoint."""
    results = []
    page = 1

    while True:
        batch = github(
            "api",
            endpoint,
            "-f", "per_page=100",
            "-f", f"page={page}",
            "--method", "GET",
        )

        if not isinstance(batch, list):
            raise RuntimeError(f"Expected a list from {endpoint}")

        results.extend(batch)

        if len(batch) < 100:
            break

        page += 1

    return results

def merged_pull_requests(repository):
    """Retrieve merged pull requests from GitHub."""
    prs = github_paginated(
        f"repos/{repository}/pulls?state=closed"
    )

    return [
        pr for pr in prs
        if pr.get("merged_at")
        and pr.get("head", {}).get("repo")
        and pr.get("base", {}).get("repo")
        and pr["head"]["repo"]["full_name"] == repository
        and pr["base"]["repo"]["full_name"] == repository
    ]

def feature_release_prs(feature_branch, merged_prs):
    """Find merged PRs promoting a feature into a release branch."""
    return [
        pr for pr in merged_prs
        if pr.get("head", {}).get("ref") == feature_branch
        and pr.get("base", {}).get("ref", "").startswith("release-")
    ]


def verify_release_chain(feature_pr, merged_prs):
    """Verify that a feature promotion reached main through a release PR."""

    release_branch = feature_pr["base"]["ref"]
    feature_merge_sha = feature_pr.get("merge_commit_sha")

    # A merged feature-to-release PR must have a resulting commit.
    if not feature_merge_sha:
        return None

    # Verify the feature's squash/merge result is in main's history.
    if not is_ancestor(feature_merge_sha, "origin/main"):
        return None

    # Find release-to-main PRs for the same release branch.
    release_prs = [
        pr for pr in merged_prs
        if pr.get("head", {}).get("ref") == release_branch
        and pr.get("base", {}).get("ref") == "main"
    ]

    for release_pr in release_prs:
        release_merge_sha = release_pr.get("merge_commit_sha")

        if not release_merge_sha:
            continue

        # Verify the release merge reached main.
        if not is_ancestor(release_merge_sha, "origin/main"):
            continue

        # A normal merge commit has two parents:
        # parents[1] = main before the merge
        # parents[2] = release branch at merge time
        parents = git(
            "rev-list", "--parents", "-n", "1", release_merge_sha
        ).split()

        if len(parents) != 3:
            continue

        release_tip_at_merge = parents[2]

        # Confirm the feature's squash commit was included
        # in the release branch at the time of merging.
        if not is_ancestor(feature_merge_sha, release_tip_at_merge):
            continue

        return {
            "feature_pr": feature_pr["number"],
            "release_pr": release_pr["number"],
            "release_branch": release_branch,
            "feature_merge_sha": feature_merge_sha,
            "release_merge_sha": release_merge_sha,
        }

    return None

def promoted_feature_revision(repository, feature_pr):
    """Return the last commit recorded in a feature-to-release PR."""

    commits = github_paginated(
        f"repos/{repository}/pulls/{feature_pr['number']}/commits"
    )

    if not commits:
        return None

    return commits[-1]["sha"]


def check_post_release_changes(feature_branch, promoted_sha):
    """Check whether a feature branch changed after its released revision."""

    current_ref = f"origin/{feature_branch}"

    # A feature branch may legitimately have been deleted after release.
    exists = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet",
         f"refs/remotes/{current_ref}"],
        capture_output=True,
    )

    if exists.returncode == 1:
        return "deleted", []

    if exists.returncode != 0:
        raise RuntimeError(
            f"Unable to check feature branch reference: {current_ref}"
        )

    current_sha = git("rev-parse", current_ref)

    if current_sha == promoted_sha:
        return "unchanged", []

    if is_ancestor(promoted_sha, current_sha):
        commits = git(
            "log", "--oneline",
            f"{promoted_sha}..{current_sha}",
        ).splitlines()

        return "additional_commits", commits

    return "diverged", []

def classify_feature_release(repository, feature_branch, merged_prs):
    """Classify a feature's release status using verified PR history."""

    release_prs = feature_release_prs(feature_branch, merged_prs)

    if not release_prs:
        return {
            "status": "not_confirmed_released",
            "feature_branch": feature_branch,
            "reason": "No merged feature-to-release PR found",
        }

    verified = []

    for feature_pr in release_prs:
        chain = verify_release_chain(feature_pr, merged_prs)

        if chain:
            verified.append((feature_pr, chain))

    if not verified:
        return {
            "status": "review",
            "feature_branch": feature_branch,
            "reason": "Release PR exists but promotion to main was not verified",
        }

    # Use the latest verified promotion, not simply the latest PR.
    feature_pr, chain = max(
        verified,
        key=lambda item: item[0]["merged_at"],
    )

    promoted_sha = promoted_feature_revision(repository, feature_pr)

    if not promoted_sha:
        return {
            "status": "review",
            "feature_branch": feature_branch,
            "reason": "Could not identify the promoted feature revision",
        }

    branch_status, extra_commits = check_post_release_changes(
        feature_branch,
        promoted_sha,
    )

    if branch_status not in ("unchanged", "deleted"):
        return {
            "status": "review",
            "feature_branch": feature_branch,
            "reason": f"Post-release branch status: {branch_status}",
            "extra_commits": extra_commits,
            "chain": chain,
        }

    return {
        "status": "released",
        "feature_branch": feature_branch,
        "chain": chain,
        "promoted_sha": promoted_sha,
        "branch_status": branch_status,
    }

def is_ancestor(commit, branch):
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, branch],
        capture_output=True,
    )

    if result.returncode not in (0, 1):
        raise RuntimeError(f"Ancestry check failed: {commit} -> {branch}")

    return result.returncode == 0


def staging_feature_branches(preserved):
    """Identify unique feature branches containing staging-only commits."""

    branches = set()

    for _, matching_branches in preserved:
        for branch in matching_branches:
            if branch.startswith("origin/feature/"):
                branches.add(branch.removeprefix("origin/"))

    return sorted(branches)

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

    # Identify candidate feature branches from staging-only commits.
    feature_branches = staging_feature_branches(preserved)

    # Retrieve PR history once, then classify each feature.
    merged_prs = merged_pull_requests(repository)

    release_results = [
        classify_feature_release(repository, branch, merged_prs)
        for branch in feature_branches
    ]

    release_reviews = any(
        item["status"] == "review"
        for item in release_results
    )

    if missing:
        result = "Blocked - unpreserved commits detected"
    elif review_merges:
        result = "Manual review required - merge differences detected"
    elif release_reviews:
        result = "Manual review required - release history needs investigation"
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


    release_section = [
        "",
        "### Release awareness",
        "",
    ]

    for item in release_results:
        branch = item["feature_branch"]
        status = item["status"]

        if status == "released":
            chain = item["chain"]

            release_section.extend([
                f"- **{branch}: Released to main**",
                f"  - Feature-to-release PR: #{chain['feature_pr']}",
                f"  - Release-to-main PR: #{chain['release_pr']}",
                "  - Post-release changes: None detected",
            ])

        elif status == "not_confirmed_released":
            release_section.extend([
                f"- **{branch}: Not confirmed released**",
                f"  - {item['reason']}",
            ])

        else:
            release_section.extend([
                f"- **{branch}: REVIEW REQUIRED**",
                f"  - {item['reason']}",
            ])

            for commit in item.get("extra_commits", []):
                release_section.append(f"  - {commit}")

    release_section.append("")

    # Place release awareness before the Important section.
    important_index = report.index("### Important")
    report[important_index:important_index] = release_section

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
