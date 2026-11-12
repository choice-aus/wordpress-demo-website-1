#!/usr/bin/env python3
"""Delete one remote branch only when its SHA matches the approved SHA."""

import argparse
import re
import subprocess
import sys
from branch_audit import get_cleanup_candidates, run
from urllib.parse import urlparse

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

def verify_remote_repository(repository):
    """Ensure origin's push URL matches the repository being audited."""

    remote_url = run("git", "remote", "get-url", "--push", "origin")

    if remote_url.startswith("git@github.com:"):
        remote_path = remote_url.split(":", 1)[1]

    elif remote_url.startswith("ssh://git@github.com/"):
        remote_path = urlparse(remote_url).path.lstrip("/")

    elif remote_url.startswith("https://github.com/"):
        remote_path = urlparse(remote_url).path.lstrip("/")

    else:
        sys.exit(f"REFUSED: Unsupported Git remote: {remote_url}")

    remote_repository = re.sub(r"\.git$", "", remote_path)

    if remote_repository.lower() != repository.lower():
        sys.exit(
            f"REFUSED: Remote repository mismatch. "
            f"Expected {repository}, found {remote_repository}"
        )

    print(f"Remote repository verified: {repository}", flush=True)

def verify_cleanup_eligibility(branch, expected_sha, completed_release):
    """Independently verify a branch is eligible for cleanup."""

    repository = run(
        "gh", "repo", "view",
        "--json", "nameWithOwner",
        "--jq", ".nameWithOwner",
    )

    verify_remote_repository(repository)

    release_verified, assessments = get_cleanup_candidates(
        repository,
        completed_release,
    )

    if not release_verified:
        sys.exit("REFUSED: Release verification failed.")

    matches = [
        item for item in assessments
        if item["branch"] == branch
    ]

    if len(matches) != 1:
        sys.exit("REFUSED: Branch is not associated with this release.")

    assessment = matches[0]

    if assessment["status"] != "Cleanup candidate":
        sys.exit(
            f"REFUSED: {assessment['status']} - {assessment['reason']}"
        )

    if assessment["sha"] != expected_sha:
        sys.exit("REFUSED: Branch SHA differs from approved SHA.")

    print(
        f"Eligibility verified: {assessment['reason']}",
        flush=True,
    )

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--branch", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument(
        "--completed-release",
        required=True,
        help="Completed release used to verify cleanup eligibility",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete the branch (default is dry-run)",
    )

    args = parser.parse_args()

    branch = args.branch
    expected_sha = args.expected_sha.lower()

    # Only feature and release branches are eligible.
    if not (
        branch.startswith("feature/")
        or branch.startswith("release-")
    ):
        sys.exit(f"REFUSED: Branch type not eligible: {branch}")

    # Reject invalid branch names and Git refspec injection.
    if subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        capture_output=True,
    ).returncode != 0:
        sys.exit(f"REFUSED: Invalid branch name: {branch}")

    if not SHA_PATTERN.fullmatch(expected_sha):
        sys.exit("REFUSED: Expected SHA must be a full 40-character SHA-1.")

    # Recheck eligibility using current GitHub and Git evidence.
    # This applies to both dry-run and execute modes.
    verify_cleanup_eligibility(
        branch=branch,
        expected_sha=expected_sha,
        completed_release=args.completed_release,
    )

    ref = f"refs/heads/{branch}"

    command = [
        "git",
        "push",
        f"--force-with-lease={ref}:{expected_sha}",
        "origin",
        f":{ref}",
    ]

    if not args.execute:
        command.insert(2, "--dry-run")

    print(f"Branch: {branch}")
    print(f"Expected SHA: {expected_sha}")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}", flush=True)

    result = subprocess.run(command, check=False)

    if result.returncode != 0:
        sys.exit(f"REFUSED/FAILED: Git push returned {result.returncode}")

    print(
        "Deletion completed."
        if args.execute
        else "Dry run completed. No branch was deleted."
    )


if __name__ == "__main__":
    main()
