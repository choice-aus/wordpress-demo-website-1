#!/usr/bin/env python3
"""Check the same commit-title rule in Git hooks and GitHub Actions."""

import argparse
from pathlib import Path
import re
import subprocess
import sys


TITLE_PATTERN = re.compile(r"\[[A-Z]{3}-[0-9]+\] - \S[^\r\n]*")
ZERO_SHA = "0" * 40


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def check_title(title):
    if not TITLE_PATTERN.fullmatch(title):
        raise ValueError(f"Invalid title: {title!r}\nExpected [ABC-123] - Description")


def check_range(revision_range):
    # Git-generated merge titles are exempt. Squash commits have one parent
    # and are checked like every other ordinary commit.
    commits = git("log", "--no-merges", "--format=%H%x00%s", revision_range, "--")
    for line in commits.splitlines():
        sha, title = line.split("\0", 1)
        try:
            check_title(title)
        except ValueError as error:
            raise ValueError(f"Commit {sha}: {error}") from error


def check_new_branch(head, remote):
    main = f"refs/remotes/{remote}/main"
    # Exclude existing production history rather than rejecting historical
    # titles when a new work branch is first published.
    git("rev-parse", "--verify", f"{main}^{{commit}}")
    check_range(f"{main}..{head}")


def check_pre_push(remote, lines):
    for line in lines:
        local_ref, local_sha, _, remote_sha = line.split()
        if local_sha == ZERO_SHA or not local_ref.startswith("refs/heads/"):
            continue  # Branch deletion and tags introduce no branch commits.
        if remote_sha == ZERO_SHA:
            check_new_branch(local_sha, remote)
        else:
            check_range(f"{remote_sha}..{local_sha}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--message-file", type=Path)
    mode.add_argument("--title")
    mode.add_argument("--range", dest="revision_range")
    mode.add_argument("--base")
    mode.add_argument("--new-branch", metavar="HEAD")
    mode.add_argument("--pre-push", metavar="REMOTE")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--remote", default="origin")
    args = parser.parse_args()
    try:
        if args.message_file is not None:
            merge_head = Path(git("rev-parse", "--git-path", "MERGE_HEAD"))
            if merge_head.exists():
                print("Merge commit: generated title exempt.")
                return 0
            message = args.message_file.read_text(encoding="utf-8")
            check_title(message.splitlines()[0] if message else "")
        elif args.title is not None:
            check_title(args.title)
        elif args.revision_range is not None:
            check_range(args.revision_range)
        elif args.base is not None:
            base = git("merge-base", args.base, args.head)
            check_range(f"{base}..{args.head}")
        elif args.new_branch is not None:
            check_new_branch(args.new_branch, args.remote)
        else:
            check_pre_push(args.pre_push, sys.stdin)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Commit title check failed: {error}", file=sys.stderr)
        print("Reword invalid commits before pushing. If a base object is missing, fetch the remote first.",
              file=sys.stderr)
        return 1
    print("Commit title check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
