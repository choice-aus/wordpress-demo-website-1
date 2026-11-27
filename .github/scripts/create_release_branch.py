#!/usr/bin/env python3
"""Create a named release branch from current main, without moving existing refs."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from branch_audit import github
from release_cycle import matching_refs, remote_sha, require, validate_next_release
from release_checks import push_ci_runs, wait_for_ci


def create_release_branch(repository, branch, expected_main=None, revalidate=None):
    validate_next_release(branch, "")
    require(not matching_refs(repository, branch),
            f"{branch} already exists; no branch was moved. Choose a new name or use the existing branch.")
    main_sha = remote_sha(repository, "main")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", main_sha)), "Main did not resolve to a full commit SHA")
    require(expected_main is None or main_sha == expected_main,
            "Main changed from the approved release-cycle SHA")

    def check_refs():
        require(remote_sha(repository, "main") == main_sha,
                "Main changed while preparing branch creation; rerun from current main")
        require(not matching_refs(repository, branch),
                f"{branch} already exists; no branch was moved")
        if revalidate is not None:
            revalidate()

    deadline = time.monotonic() + 600
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        require(remaining > 0, "Timed out waiting to create release branch")
        wait_for_ci(repository, main_sha, check_refs, timeout=remaining)
        check_refs()
        # Create-only POST cannot overwrite a concurrently created branch.
        try:
            created = github("api", f"repos/{repository}/git/refs", "--method", "POST",
                             "-f", f"ref=refs/heads/{branch}", "-f", f"sha={main_sha}")
            break
        except subprocess.CalledProcessError as error:
            # A push event can become visible after our final check. Retry once
            # only for HTTP 422 with positively observed active same-SHA CI.
            # Auth/network errors and permanent failures are not retried.
            if attempt or 'HTTP 422' not in (error.stderr or ''):
                raise
            check_refs()
            if not any(run['status'] != 'completed' for run in push_ci_runs(repository, main_sha)):
                raise
            print('CI appeared after the readiness check; waiting and retrying create-only once', flush=True)
    require(created.get("ref") == f"refs/heads/{branch}"
            and created.get("object", {}).get("sha") == main_sha,
            "Unexpected create response; inspect the remote branch before retrying")
    return dict(repository=repository, branch=branch, source="main", sha=main_sha)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_branch", help="New release-* branch name, for example release-dev-228")
    args = parser.parse_args()
    repository = os.environ["GITHUB_REPOSITORY"]
    # The Actions entry point executes reviewed code from main. Local tests and
    # explicit CLI usage need no GITHUB_REF environment variable.
    if os.environ.get("GITHUB_REF"):
        require(os.environ["GITHUB_REF"] == "refs/heads/main", "Run Create release branch from main")
    result = create_release_branch(repository, args.release_branch)
    Path("release-branch-creation.json").write_text(json.dumps(result, indent=2) + "\n")
    report = (f"Created `{result['branch']}` from main at `{result['sha']}`.\n\n"
              "No VIP environment settings were changed. This creates a Git branch, not a GitHub Release.\n")
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(report)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(f"BLOCKED: {error}")
