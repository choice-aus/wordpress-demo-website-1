"""Shared branch and promotion policy for local tools and Actions."""

import argparse


WORK_BRANCH_PREFIXES = ("feature/", "fix/", "hotfix/", "chore/")


def is_work_branch(branch):
    return any(branch.startswith(prefix) and len(branch) > len(prefix)
               for prefix in WORK_BRANCH_PREFIXES)


def promotion_error(source, target):
    if target == "main" and not (source.startswith("release-") and len(source) > len("release-")):
        return "Only release-* branches may merge into main."
    if target.startswith("release-") and not is_work_branch(source):
        return "Only feature/*, fix/*, hotfix/* or chore/* branches may merge into release-*."
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    error = promotion_error(args.source, args.target)
    if error:
        parser.exit(1, error + "\n")
    print(f"Allowed PR: {args.source} -> {args.target}")


if __name__ == "__main__":
    main()
