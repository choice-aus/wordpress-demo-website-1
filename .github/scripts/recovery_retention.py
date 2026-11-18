#!/usr/bin/env python3
"""Assess and prune proven redundant recovery refs after a separate approval."""

import argparse
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

from branch_audit import git, github, is_ancestor
from delete_branch import verify_remote_repository
from release_cycle import approval_environment, require
from staging_reset_check import check_merge_commits, merged_pull_requests
from staging_reset_plan import released_feature_commits

SHA = re.compile(r"[0-9a-f]{40}")
EVIDENCE_NAME = re.compile(r"release-cycle-evidence-(\d+)")
DEFAULT_MIN_DAYS = 30


def validate_policy(keep_cycles, min_days):
    require(type(keep_cycles) is int and keep_cycles >= 3,
            "Retention must keep at least 3 successful cycles")
    require(type(min_days) is int and min_days >= 1,
            "Retention days must be a positive whole number")


def paginated(endpoint):
    items = []
    page = 1
    while True:
        separator = "&" if "?" in endpoint else "?"
        batch = github("api", f"{endpoint}{separator}per_page=100&page={page}")
        # Both endpoints return a named collection rather than a bare array.
        batch = batch["workflow_runs"] if "workflow_runs" in batch else batch["artifacts"]
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


def read_evidence(repository, artifact):
    require(artifact["size_in_bytes"] <= 8 * 1024 * 1024, "Recovery evidence archive is unexpectedly large")
    try:
        data = subprocess.check_output([
            "gh", "api", f"repos/{repository}/actions/artifacts/{artifact['id']}/zip"],
            stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as error:
        # gh can include a signed artifact-download URL in its error. Keep it
        # out of logs, and never turn download failure into deletion evidence.
        raise RuntimeError(f"Cannot download recovery evidence artifact {artifact['id']}; "
                           "check network/access and rerun assessment. No deletion is authorised.") from error
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        require(sum(item.file_size for item in archive.infolist()) <= 32 * 1024 * 1024,
                "Expanded recovery evidence is unexpectedly large")
        return {name: archive.read(name) for name in (
            "closure-receipt.json", "release-cycle-plan.json", "carryover-manifest.json",
            "release-cycle-result.txt") if name in archive.namelist()}


def decode_cycle(repository, run_id, attempt, files, completed_at):
    """Accept receipts or the equivalent full-success evidence from DEV-219/220."""
    if "closure-receipt.json" in files:
        cycle = json.loads(files["closure-receipt.json"])
        require(cycle.get("schema") == 1, "Unknown closure receipt schema")
        require(str(cycle["run_id"]) == str(run_id) and str(cycle["run_attempt"]) == str(attempt),
                "Closure receipt run mismatch")
        source = "closure-receipt"
    else:
        plan = json.loads(files["release-cycle-plan.json"])
        manifest = json.loads(files["carryover-manifest.json"])
        output = files["release-cycle-result.txt"].decode()
        candidate = manifest["candidate"]
        candidate_sha = manifest["candidate_sha"]
        require(plan.get("prepare") is True and not manifest.get("failed"), "Preparation was not a full success")
        require("Preparation only:" not in output, "Preparation-only evidence must be retained")
        require(f"Staging promoted to verified candidate {candidate} at {candidate_sha}" in output,
                "No completed staging promotion in legacy evidence")
        deleted = [line.removeprefix("Deleted verified branches: ").split(", ")
                   for line in output.splitlines() if line.startswith("Deleted verified branches: ")]
        require(deleted and set(plan["cleanup"]) == set(deleted[-1])
                and plan["release"] in deleted[-1], "No completed branch cleanup in legacy evidence")
        cycle = dict(repository=plan["repository"], release=plan["release"], main=plan["main"],
                     original_staging=plan["staging"], backup_sha=plan["staging"],
                     backup=f"backup/staging-before-reset-{plan['staging'][:12]}",
                     candidate=candidate, candidate_sha=candidate_sha)
        source = "legacy-full-success-evidence"
    require(cycle["repository"] == repository, "Closure receipt repository mismatch")
    for field in ("main", "original_staging", "backup_sha", "candidate_sha"):
        require(bool(SHA.fullmatch(cycle[field])), f"Invalid closure SHA: {field}")
    require(cycle["backup_sha"] == cycle["original_staging"], "Backup SHA mismatch")
    require(cycle["candidate"] == f"staging-next-cycle-{run_id}-{attempt}"
            or (str(attempt) == "1" and cycle["candidate"] == f"staging-next-cycle-{run_id}"),
            "Unexpected candidate name")
    require(cycle["backup"] == f"backup/staging-before-reset-{cycle['original_staging'][:12]}",
            "Unexpected backup name")
    return dict(run_id=str(run_id), run_attempt=str(attempt), source=source,
                completed_at=completed_at, backup=cycle["backup"], backup_sha=cycle["backup_sha"],
                candidate=cycle["candidate"], candidate_sha=cycle["candidate_sha"])


def completed_cycles(repository):
    runs = {run["id"]: run for run in paginated(
        f"repos/{repository}/actions/workflows/branch-cleanup.yml/runs")}
    cycles, protected = [], set()
    for run_id in sorted(runs):
        artifacts = paginated(f"repos/{repository}/actions/runs/{run_id}/artifacts")
        for artifact in artifacts:
            match = EVIDENCE_NAME.fullmatch(artifact["name"])
            if not match or artifact["expired"]:
                continue
            attempt = match[1]
            files = read_evidence(repository, artifact)
            # Failure and preparation-only evidence can share a backup ref
            # with a successful cycle. Protect it as well as their candidate.
            related = {f"staging-next-cycle-{run_id}-{attempt}"}
            if "release-cycle-plan.json" in files:
                plan = json.loads(files["release-cycle-plan.json"])
                if plan.get("prepare") and plan.get("staging") and SHA.fullmatch(plan["staging"]):
                    related.add(f"backup/staging-before-reset-{plan['staging'][:12]}")
            if "carryover-manifest.json" in files:
                related.add(json.loads(files["carryover-manifest.json"])["candidate"])
            attempt_info = github("api", f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}")
            try:
                require(attempt_info["conclusion"] == "success", "Run attempt did not succeed")
                cycles.append(decode_cycle(repository, run_id, attempt, files, artifact["created_at"]))
            except (RuntimeError, KeyError, ValueError):
                protected.update(related)
    return cycles, protected


def recovery_refs():
    git("fetch", "origin", "+refs/heads/*:refs/remotes/origin/*", "--prune")
    output = git("for-each-ref", "--format=%(refname:strip=3) %(objectname)", "refs/remotes/origin/")
    return {branch: sha for branch, sha in (line.split() for line in output.splitlines())
            if branch in ("main", "staging") or branch.startswith(("backup/", "staging-next-cycle"))}


def preservation_checker(repository, main_sha, staging_sha):
    """Do not prune a ref unless its content is still accounted for today."""
    released = None
    def preserved(sha):
        nonlocal released
        if is_ancestor(sha, main_sha) or is_ancestor(sha, staging_sha):
            return True
        if check_merge_commits(sha)[1]:
            return False
        if released is None:
            released = released_feature_commits(repository, merged_pull_requests(repository))
        commits = git("rev-list", "--no-merges", sha, f"^{main_sha}", f"^{staging_sha}").splitlines()
        return all(commit in released for commit in commits)
    return preserved


def build_plan(repository, refs, cycles, protected, assessed_at, preserved,
               keep_cycles=3, min_days=DEFAULT_MIN_DAYS):
    validate_policy(keep_cycles, min_days)
    now = datetime.fromisoformat(assessed_at)
    require(now.tzinfo is not None, "Retention timestamp must include a timezone")
    cycles = sorted(cycles, key=lambda item: (item["completed_at"], item["run_id"], item["run_attempt"]), reverse=True)
    newest = {(item["run_id"], item["run_attempt"]) for item in cycles[:keep_cycles]}
    associations = {}
    for cycle in cycles:
        for kind in ("backup", "candidate"):
            associations.setdefault(cycle[kind], []).append((cycle, cycle[kind + "_sha"]))
    assessments, cleanup = [], {}
    for branch, sha in sorted(refs.items()):
        if branch in ("main", "staging"):
            continue
        evidence = associations.get(branch, [])
        if branch in protected:
            reason = "Referenced by failed, preparation-only or unverified run evidence"
        elif not evidence:
            reason = "No available verified completed-cycle evidence; retain legacy/active recovery ref"
        elif any(expected != sha for _, expected in evidence):
            reason = "Current SHA differs from completed-cycle evidence"
        elif any((item["run_id"], item["run_attempt"]) in newest for item, _ in evidence):
            reason = f"Retained among the latest {keep_cycles} successful cycles"
        elif any(now - datetime.fromisoformat(item["completed_at"].replace("Z", "+00:00"))
                 < timedelta(days=min_days) for item, _ in evidence):
            reason = f"Retained for at least {min_days} days"
        elif not preserved(sha):
            reason = "Recovery content is not fully accounted for in current main/staging or verified releases"
        else:
            reason = "Verified completed cycle outside retention; recovery content remains accounted for"
            cleanup[branch] = sha
        assessments.append(dict(branch=branch, sha=sha,
                                status="Cleanup candidate" if branch in cleanup else "Keep", reason=reason))
    return dict(schema=1, repository=repository, assessed_at=assessed_at, keep_cycles=keep_cycles,
                min_days=min_days, refs=refs, cycles=cycles, protected=sorted(protected),
                assessments=assessments, cleanup=cleanup)


def assess(repository, assessed_at, keep_cycles, min_days):
    validate_policy(keep_cycles, min_days)
    refs = recovery_refs()
    require("main" in refs and "staging" in refs, "Main and staging refs are required")
    cycles, protected = completed_cycles(repository)
    return build_plan(repository, refs, cycles, protected, assessed_at,
                      preservation_checker(repository, refs["main"], refs["staging"]), keep_cycles, min_days)


def execute(plan):
    require(plan["repository"] == os.environ["GITHUB_REPOSITORY"], "Retention plan repository mismatch")
    approval_environment(plan["repository"])
    current = assess(plan["repository"], plan["assessed_at"], plan["keep_cycles"], plan["min_days"])
    require(current == plan, "Recovery refs, evidence or preservation changed; rerun retention assessment")
    if not plan["cleanup"]:
        print("No recovery refs are eligible for deletion.")
        return
    verify_remote_repository(plan["repository"])
    command = ["git", "push", "--atomic"]
    command.extend(f"--force-with-lease=refs/heads/{branch}:{sha}"
                   for branch, sha in sorted(plan["cleanup"].items()))
    command.append("origin")
    command.extend(f":refs/heads/{branch}" for branch in sorted(plan["cleanup"]))
    subprocess.run(command, check=True)
    print("Deleted redundant recovery refs: " + ", ".join(sorted(plan["cleanup"])))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("assess", "execute"))
    parser.add_argument("--plan", default="retention-plan.json")
    parser.add_argument("--keep-cycles", type=int, default=3)
    parser.add_argument("--min-days", type=int, default=DEFAULT_MIN_DAYS,
                        help="Minimum recovery-ref age in days (default: 30; use 3 for demo assessment)")
    args = parser.parse_args()
    repository = os.environ["GITHUB_REPOSITORY"]
    if args.action == "execute":
        execute(json.loads(Path(args.plan).read_text()))
        return
    approval_environment(repository)
    plan = assess(repository, datetime.now(timezone.utc).isoformat(), args.keep_cycles, args.min_days)
    Path(args.plan).write_text(json.dumps(plan, indent=2) + "\n")
    print(f"Retention: keep {args.keep_cycles} latest successful cycles and at least {args.min_days} days.")
    for item in plan["assessments"]:
        print(f"- {item['branch']} at {item['sha']}: {item['status']} — {item['reason']}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, ValueError, KeyError) as error:
        sys.exit(f"BLOCKED: {error}")
