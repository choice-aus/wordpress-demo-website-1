"""Conservative content proof for a squash-promoted PR."""

import subprocess
from branch_audit import git, is_ancestor


def verify_promoted_pr(pr, main_ref="origin/main"):
    """Compare the whole reviewed feature diff with its deployed squash diff.

    Exact binary diffs retain whitespace, modes, deletions and binary changes.
    Different base content/conflict resolutions require manual review rather
    than accepting a weaker per-commit or whitespace-normalised patch match.
    """
    squash = pr.get("merge_commit_sha")
    head = pr.get("head", {}).get("sha")
    if not squash or not head or not pr.get("merged_at"):
        return False
    if not is_ancestor(squash, main_ref):
        return False
    if subprocess.run(["git", "cat-file", "-e", f"{head}^{{commit}}"],
                      capture_output=True).returncode:
        # Deleted/rewritten branches can leave the reviewed head outside the
        # normal clone. Fetch its immutable object, never the current branch tip.
        if subprocess.run(["git", "fetch", "origin", head],
                          capture_output=True).returncode:
            return False
    parents = git("rev-list", "--parents", "-n", "1", squash).split()
    if len(parents) != 2:  # Feature promotions must be squash commits.
        return False
    bases = git("merge-base", "--all", parents[1], head).splitlines()
    if len(bases) != 1:
        return False

    def diff(start, end):
        return subprocess.check_output([
            "git", "-c", "diff.external=", "diff", "--no-ext-diff",
            "--no-textconv", "--binary", "--full-index", "--no-renames",
            start, end, "--",
        ])

    original = diff(bases[0], head)
    return bool(original) and original == diff(parents[1], squash)
