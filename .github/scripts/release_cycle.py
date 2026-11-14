#!/usr/bin/env python3
"""Snapshot, prepare and close a release cycle with fail-closed checks."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from branch_audit import get_cleanup_candidates, get_latest_release, git, github
from delete_branch import verify_remote_repository
from staging_reset_plan import safety_summary, reset_decision, open_staging_pr_count
import uat_carryover
import verify_next_cycle


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def approval_environment(repository):
    environment = github("api", f"repos/{repository}/environments/branch-cleanup")
    require(any(rule.get("type") == "required_reviewers" and rule.get("reviewers")
                for rule in environment.get("protection_rules", [])),
            "branch-cleanup must have required reviewers configured")


def snapshot(repository, release):
    latest = get_latest_release(repository)
    verified, assessments = get_cleanup_candidates(repository, release)
    require(verified, "Completed release is not verified in main")
    return {
        "repository": repository,
        "release": release,
        "latest": latest,
        "prepare": release == latest,
        "main": git("rev-parse", "origin/main"),
        "staging": git("rev-parse", "origin/staging"),
        "release_sha": git("rev-parse", f"origin/{release}"),
        "cleanup": {item["branch"]: item["sha"] for item in assessments
                    if item["status"] == "Cleanup candidate"},
    }


def revalidate(plan, staging=None):
    current = snapshot(plan["repository"], plan["release"])
    expected = dict(plan)
    if staging:
        expected["staging"] = staging
    require(current == expected, "Release eligibility or branch SHAs changed; rerun assessment")
    approval_environment(plan["repository"])
    return current


def remote_sha(repository, branch):
    return github("api", f"repos/{repository}/git/ref/heads/{branch}")["object"]["sha"]


def create_or_verify(repository, branch, sha):
    # A failed read is not evidence of absence (auth/network errors must abort).
    refs = github("api", f"repos/{repository}/git/matching-refs/heads/{branch}")
    matches = [ref for ref in refs if ref["ref"] == f"refs/heads/{branch}"]
    if matches:
        require(matches[0]["object"]["sha"] == sha, f"Unexpected existing SHA: {branch}")
    else:
        github("api", f"repos/{repository}/git/refs", "--method", "POST",
               "-f", f"ref=refs/heads/{branch}", "-f", f"sha={sha}")


def check_preparation_safety(plan):
    decision, reason = reset_decision(safety_summary(plan["staging"]), open_staging_pr_count())
    require(decision in ("Ready for approval", "Approval required"), reason)


def prepare(plan):
    require(plan["prepare"], "Only the latest completed release may prepare staging")
    revalidate(plan)
    check_preparation_safety(plan)
    revalidate(plan)
    repository = plan["repository"]
    backup = f"backup/staging-before-reset-{plan['staging'][:12]}"
    # Each attempt is independent; a rerun never overwrites an earlier candidate.
    candidate = f"staging-next-cycle-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
    create_or_verify(repository, backup, plan["staging"])
    revalidate(plan)
    create_or_verify(repository, candidate, plan["main"])
    os.environ.update(EXPECTED_MAIN=plan["main"], EXPECTED_STAGING=plan["staging"],
                      BACKUP_BRANCH=backup, CANDIDATE_BRANCH=candidate)
    uat_carryover.REPO = repository
    require(uat_carryover.main() == 0, "Carryover preparation failed")
    require(verify_next_cycle.main() == 0, "Candidate verification failed; staging unchanged")
    manifest = json.loads(Path("carryover-manifest.json").read_text())
    return candidate, manifest["candidate_sha"], backup


def reset_staging(plan, candidate, candidate_sha, backup):
    require(plan["prepare"], "Older releases cannot reset staging")
    revalidate(plan)
    require(open_staging_pr_count() == 0, "Open staging PRs block reset")
    require(remote_sha(plan["repository"], candidate) == candidate_sha, "Candidate SHA changed")
    require(remote_sha(plan["repository"], backup) == plan["staging"], "Backup SHA changed")
    # Verify content/accounting again, immediately before the leased update.
    require(verify_next_cycle.main() == 0, "Candidate no longer verifies")
    revalidate(plan)
    verify_remote_repository(plan["repository"])
    git("push", f"--force-with-lease=refs/heads/staging:{plan['staging']}",
        "origin", f"{candidate_sha}:refs/heads/staging")
    print(f"Staging promoted to verified candidate {candidate} at {candidate_sha}")


def cleanup(plan, staging=None):
    revalidate(plan, staging)
    require(plan["release"] in plan["cleanup"], "Release itself must remain cleanup eligible")
    verify_remote_repository(plan["repository"])
    # One atomic push prevents a half-deleted release and stale target SHAs.
    # Each branch is independently content-verified by get_cleanup_candidates.
    command = ["git", "push", "--atomic"]
    for branch, sha in sorted(plan["cleanup"].items()):
        command.append(f"--force-with-lease=refs/heads/{branch}:{sha}")
    command.append("origin")
    command.extend(f":refs/heads/{branch}" for branch in sorted(plan["cleanup"]))
    subprocess.run(command, check=True)
    print("Deleted verified branches: " + ", ".join(sorted(plan["cleanup"])))


def execute(plan, preparation_only=True):
    revalidate(plan)
    promoted = None
    if plan["prepare"]:
        candidate, sha, backup = prepare(plan)
        if not preparation_only:
            reset_staging(plan, candidate, sha, backup)
            promoted = sha
    else:
        print("Older completed release: cleanup only; preparation and staging reset disabled")
    if preparation_only:
        print("Preparation only: staging reset and branch deletion disabled")
        return
    cleanup(plan, promoted)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("assess", "execute"))
    parser.add_argument("--plan", default="release-cycle-plan.json")
    parser.add_argument("--audit-mode", choices=("latest", "specific"), default="latest")
    parser.add_argument("--completed-release", default="")
    parser.add_argument("--preparation-only", choices=("true", "false"), default="true")
    args = parser.parse_args()
    repository = os.environ["GITHUB_REPOSITORY"]
    if args.action == "assess":
        approval_environment(repository)
        require(args.audit_mode == "specific" or not args.completed_release,
                "completed_release is only valid in specific mode")
        release = args.completed_release if args.audit_mode == "specific" else get_latest_release(repository)
        require(release.startswith("release-"), "A release-* branch is required")
        plan = snapshot(repository, release)
        if plan["prepare"]:
            check_preparation_safety(plan)
        Path(args.plan).write_text(json.dumps(plan, indent=2) + "\n")
        print("Review this snapshot before approving branch-cleanup:\n")
        print(json.dumps(plan, indent=2))
    else:
        plan = json.loads(Path(args.plan).read_text())
        require(plan["repository"] == repository, "Plan repository mismatch")
        execute(plan, args.preparation_only == "true")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(f"BLOCKED: {error}")
