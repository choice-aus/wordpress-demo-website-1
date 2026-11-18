"""Conservative content proof for a squash-promoted PR."""

import subprocess
from branch_audit import git, is_ancestor


def verify_promoted_pr(pr, main_ref="origin/main"):
    """Compare the whole reviewed feature diff with its deployed squash diff.

    Exact binary diffs retain whitespace, modes, deletions and binary changes.
    If changes already exist in the release base, accept only a clean Git
    reconstruction whose complete tree exactly matches the deployed squash.
    Conflict resolutions still require manual review.
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
    if not original:
        return False
    if original == diff(parents[1], squash):
        return True
    # Some reviewed changes may already have reached the release base through
    # another squash. Reconstruct a clean integration and require the complete
    # resulting tree (including binary blobs and modes) to equal the squash.
    # Conflict resolutions and extra release changes remain unverified.
    merge = subprocess.run(["git", "merge-tree", "--write-tree", parents[1], head],
                           capture_output=True, text=True)
    return (merge.returncode == 0
            and merge.stdout.splitlines()[0] == git("rev-parse", f"{squash}^{{tree}}"))


def revision_is_released(repository, branch, revision):
    """Verify a particular staging revision, without relying on current labels."""
    from staging_reset_check import merged_pull_requests, feature_release_prs, verify_release_chain
    history = merged_pull_requests(repository)
    for promotion in feature_release_prs(branch, history):
        if (verify_release_chain(promotion, history)
                and verify_promoted_pr(promotion)
                and is_ancestor(revision, promotion["head"]["sha"])):
            return True
    return False
