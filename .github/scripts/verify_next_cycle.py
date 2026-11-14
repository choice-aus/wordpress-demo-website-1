#!/usr/bin/env python3
"""Read-only verification of a prepared next UAT cycle."""

import json
import os
import subprocess
import sys

from staging_reset_check import git, is_ancestor
from staging_reset_plan import (
    classify_staging_commits,
    staging_feature_commits,
)
from staging_reset_check import merged_pull_requests, run
from uat_carryover import selected_carryover_prs


def main():
    main_sha = os.environ["EXPECTED_MAIN"]
    staging_sha = os.environ["EXPECTED_STAGING"]
    candidate = os.environ["CANDIDATE_BRANCH"]
    backup = os.environ["BACKUP_BRANCH"]

    subprocess.run(
        ["git", "fetch", "origin", "main", "staging", candidate, backup],
        check=True,
    )

    candidate_sha = git("rev-parse", f"origin/{candidate}")

    with open("carryover-manifest.json") as input_file:
        manifest = json.load(input_file)

    if manifest.get("candidate") != candidate:
        print("BLOCKED: Carryover manifest references a different candidate.")
        return 1

    if manifest.get("candidate_sha") != candidate_sha:
        print("BLOCKED: Candidate SHA differs from the carryover manifest.")
        return 1

    selected = selected_carryover_prs()

    expected = {
        (item["number"], item["branch"])
        for item in selected.values()
    }

    successful = {
        (item["pr"], item["branch"])
        for item in manifest["carryovers"]
    }

    failed = {
        (item["pr"], item["branch"])
        for item in manifest["failed"]
    }

    if successful & failed:
        print("BLOCKED: A carryover is recorded as both successful and failed.")
        return 1

    if successful | failed != expected:
        print("BLOCKED: Carryover manifest does not match selected PRs.")
        print(f"Missing: {sorted(expected - (successful | failed))}")
        print(f"Unexpected: {sorted((successful | failed) - expected)}")
        return 1

    for item in manifest["carryovers"]:
        selected_item = selected[item["branch"]]

        if item["tested_sha"] != selected_item["tested_sha"]:
            print(
                f"BLOCKED: Recorded revision differs for PR #{item['pr']}."
            )
            return 1

    print(
        f"Carryover accounting verified: "
        f"{len(successful)} successful, {len(failed)} flagged."
    )

    for item in manifest["carryovers"]:
        tested_sha = item["tested_sha"]

        if not is_ancestor(tested_sha, candidate_sha):
            print(
                f"BLOCKED: Carryover PR #{item['pr']} "
                f"({item['branch']}) is missing from the candidate."
            )
            return 1

    print(
        f"Verified {len(manifest['carryovers'])} "
        "recorded carryover revisions."
    )

    backup_sha = git("rev-parse", f"origin/{backup}")

    if backup_sha != staging_sha:
        print("BLOCKED: Staging backup does not match original staging.")
        print(f"Expected: {staging_sha}")
        print(f"Backup:   {backup_sha}")
        return 1

    for item in manifest["failed"]:
        print(
            f"MANUAL FOLLOW-UP: PR #{item['pr']} "
            f"({item['branch']}) could not be carried over. "
            f"Reason: {item['reason']}. Original staging is preserved at {backup}."
        )

    print(
        f"Verified original staging backup; "
        f"{len(manifest['failed'])} carryover(s) require manual attention."
    )

    current_main = git("rev-parse", "origin/main")
    current_staging = git("rev-parse", "origin/staging")

    if current_main != main_sha:
        print("BLOCKED: Main changed after the safety assessment.")
        print(f"Expected: {main_sha}")
        print(f"Current:  {current_main}")
        return 1

    if current_staging != staging_sha:
        print("BLOCKED: Staging changed after the safety assessment.")
        print(f"Expected: {staging_sha}")
        print(f"Current:  {current_staging}")
        return 1

    if not is_ancestor(main_sha, candidate_sha):
        print("BLOCKED: Candidate does not contain expected main.")
        return 1

    from staging_reset_check import check_merge_commits
    _, review_merges = check_merge_commits(staging_sha)
    if review_merges:
        print(f"BLOCKED: Staging merge content requires review: {review_merges}")
        return 1

    staging_only = git(
        "rev-list",
        "--no-merges",
        staging_sha,
        f"^{main_sha}",
    ).splitlines()

    classifications = classify_staging_commits(
        staging_sha,
        main_sha,
    )

    missing = [
        commit for commit in staging_only
        if not is_ancestor(commit, candidate_sha)
    ]

    released = [
        commit for commit in missing
        if classifications.get(commit) == "verified"
    ]

    unresolved = [
        commit for commit in missing
        if classifications.get(commit) != "verified"
    ]

    repository = run(
        "gh", "repo", "view",
        "--json", "nameWithOwner",
        "--jq", ".nameWithOwner",
    )

    merged_prs = merged_pull_requests(repository)
    staging_pr_map = staging_feature_commits(repository, merged_prs)

    failed_prs = {
        item["pr"] for item in manifest["failed"]
    }

    accounted_failures = []
    unaccounted = []

    for commit in unresolved:
        associations = staging_pr_map.get(commit, [])

        if associations and all(
            item["pr"] in failed_prs for item in associations
        ):
            accounted_failures.append(commit)
        else:
            unaccounted.append(commit)

    print("## Next-cycle candidate verification")
    print()
    print(f"- Main: `{main_sha}`")
    print(f"- Original staging: `{staging_sha}`")
    print(f"- Candidate: `{candidate}`")
    print(f"- Candidate SHA: `{candidate_sha}`")
    print(f"- Staging-only commits: {len(staging_only)}")
    print(f"- Commits not present in candidate: {len(missing)}")
    print(f"- Verified released commits: {len(released)}")
    print(f"- Unresolved commits: {len(unresolved)}")
    print(f"- Associated with failed carryovers: {len(accounted_failures)}")
    print(f"- Unaccounted staging commits: {len(unaccounted)}")

    if unresolved or failed:
        print()
        print("### Unresolved staging commits")
        for commit in unresolved:
            status = classifications.get(commit, "unknown")
            category = (
                "failed carryover"
                if commit in accounted_failures
                else "unaccounted"
            )
            print(f"- `{commit}` — {status}; {category}")

        print()
        print("**BLOCKED: Outstanding staging changes require review.**")
        return 1

    print()
    print("**PASS: No unresolved staging commits detected.**")
    return 0


if __name__ == "__main__":
    sys.exit(main())