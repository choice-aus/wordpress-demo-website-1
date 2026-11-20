#!/usr/bin/env python3
"""Create a named release branch from current main, without moving existing refs."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from branch_audit import github
from release_cycle import matching_refs, remote_sha, require, validate_next_release


def create_release_branch(repository, branch):
    validate_next_release(branch, "")
    require(not matching_refs(repository, branch),
            f"{branch} already exists; no branch was moved. Choose a new name or use the existing branch.")
    main_sha = remote_sha(repository, "main")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", main_sha)), "Main did not resolve to a full commit SHA")
    require(remote_sha(repository, "main") == main_sha,
            "Main changed while preparing branch creation; rerun from current main")
    # Creation fails if another actor creates this name after the absence check.
    created = github("api", f"repos/{repository}/git/refs", "--method", "POST",
                     "-f", f"ref=refs/heads/{branch}", "-f", f"sha={main_sha}")
    require(created.get("ref") == f"refs/heads/{branch}"
            and created.get("object", {}).get("sha") == main_sha,
            "Unexpected create response; inspect the remote branch before retrying")
    return dict(repository=repository, branch=branch, source="main", sha=main_sha)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_branch", help="New release-* branch name, for example release-dev-224")
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
