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

from branch_audit import git, github, is_ancestor, get_open_prs
from delete_branch import verify_remote_repository
from release_cycle import approval_environment, require
from staging_reset_check import check_merge_commits, merged_pull_requests
from staging_reset_plan import released_feature_commits

SHA = re.compile(r"[0-9a-f]{40}")
EVIDENCE_NAME = re.compile(r"release-cycle-evidence-(\d+)")
DEFAULT_MIN_DAYS = 30
CANDIDATE_NAME = re.compile(r"staging-next-cycle-(\d+)(?:-(\d+))?")


def ref_kind(branch):
    if branch == "staging-next-cycle" or branch.startswith("staging-next-cycle-"):
        return "candidate"
    if branch.startswith("backup/"):
        return "backup"
    return "other"


def scoped_refs(refs, scope):
    require(scope in ("candidates", "backups", "all"), "Unknown housekeeping scope")
    return {branch: sha for branch, sha in refs.items()
            if branch in ("main", "staging") or scope == "all"
            or ref_kind(branch) == ("candidate" if scope == "candidates" else "backup")}


def candidate_run_states(repository, refs):
    """Use run metadata, not expiring artifacts, to protect active preparations."""
    if not any(ref_kind(branch) == "candidate" for branch in refs):
        return {}
    preparation_paths = {".github/workflows/branch-cleanup.yml",
                         ".github/workflows/prepare-next-uat-cycle.yml"}
    candidate_ids = {match[1] for branch in refs
                     if (match := CANDIDATE_NAME.fullmatch(branch))}
    # Repository-wide history retains runs of retired workflow files. Looking up
    # the deleted preparation shortcut by filename can instead return HTTP 404.
    # Also keep exact candidate run IDs even if their workflow path is unknown.
    runs = {str(run["id"]): run for run in paginated(f"repos/{repository}/actions/runs")
            if (run.get("path") or "").split("@")[0] in preparation_paths
            or str(run["id"]) in candidate_ids}
    active = any(run["status"] != "completed" for run in runs.values())
    open_prs = {}
    for pr in get_open_prs(repository):
        head = pr.get("head", {})
        if head.get("repo") and head["repo"].get("full_name") == repository:
            open_prs.setdefault(head["ref"], []).append(pr["number"])
    states = {}
    for branch in refs:
        if ref_kind(branch) != "candidate":
            continue
        match = CANDIDATE_NAME.fullmatch(branch)
        run = runs.get(match[1]) if match else None
        if run:
            attempt = match[2] or "1"
            if str(run.get("run_attempt", 1)) != attempt:
                run = github("api", f"repos/{repository}/actions/runs/{match[1]}/attempts/{attempt}")
            states[branch] = dict(legacy=False, status=run["status"], conclusion=run.get("conclusion"),
                                  run_id=match[1], run_attempt=attempt)
        else:
            states[branch] = dict(legacy=True, status="unknown", active_preparations=active)
        states[branch]["open_prs"] = sorted(open_prs.get(branch, []))
    return states


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
            "closure-receipt.json", "promotion-receipt.json", "release-cycle-plan.json", "carryover-manifest.json",
            "release-cycle-result.txt") if name in archive.namelist()}


def verify_independent_closure(cycle, files):
    """Schema 2 proves core closure independently of optional follow-up failure."""
    plan = json.loads(files["release-cycle-plan.json"])
    manifest = json.loads(files["carryover-manifest.json"])
    promotion = json.loads(files["promotion-receipt.json"])
    require(cycle.get("closure_complete") is True, "Closure is not complete")
    require(isinstance(cycle.get("release_sha"), str) and SHA.fullmatch(cycle["release_sha"]),
            "Closure release revision is missing")
    deleted = cycle.get("deleted_branches")
    require(isinstance(deleted, dict) and deleted == plan.get("cleanup")
            and deleted.get(cycle["release"]) == cycle["release_sha"]
            and all(isinstance(sha, str) and SHA.fullmatch(sha) for sha in deleted.values())
            and not ({"main", "staging"} & set(deleted)), "Closure deletion proof differs from plan")
    require(cycle["release_sha"] == plan.get("release_sha"), "Closure release SHA differs from plan")
    require(type(cycle.get("candidate_removed")) is bool, "Missing candidate disposition")
    require(promotion.get("schema") == 1 and all(promotion.get(key) == cycle.get(key) for key in (
        "repository", "release", "main", "original_staging", "candidate", "candidate_sha", "backup",
        "backup_sha", "candidate_removed", "run_id", "run_attempt")), "Promotion receipt differs from closure")
    require(manifest.get("candidate") == cycle["candidate"]
            and manifest.get("candidate_sha") == cycle["candidate_sha"]
            and manifest.get("failed") == [], "Carryover proof differs from closure")
    output = files["release-cycle-result.txt"].decode()
    deleted_lines = [line.removeprefix("Deleted verified branches: ").split(", ")
                     for line in output.splitlines() if line.startswith("Deleted verified branches: ")]
    require(deleted_lines and set(deleted_lines[-1]) == set(deleted)
            and f"Staging promoted to verified candidate {cycle['candidate']} at {cycle['candidate_sha']}" in output
            and "Preparation only:" not in output, "No matching completed closure operations in log")


def decode_cycle(repository, run_id, attempt, files, completed_at):
    """Accept receipts or the equivalent full-success evidence from DEV-219/220."""
    if "closure-receipt.json" in files:
        cycle = json.loads(files["closure-receipt.json"])
        require(cycle.get("schema") in (1, 2), "Unknown closure receipt schema")
        require(str(cycle["run_id"]) == str(run_id) and str(cycle["run_attempt"]) == str(attempt),
                "Closure receipt run mismatch")
        source = "closure-receipt-v2" if cycle["schema"] == 2 else "closure-receipt"
        if cycle["schema"] == 2:
            verify_independent_closure(cycle, files)
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
    result = dict(run_id=str(run_id), run_attempt=str(attempt), source=source,
                completed_at=completed_at, backup=cycle["backup"], backup_sha=cycle["backup_sha"],
                candidate=cycle["candidate"], candidate_sha=cycle["candidate_sha"])
    # A later closure can resolve an older hold only with the matching reviewed
    # release revision. Older receipts without the plan remain valid retention
    # evidence, but cannot supply this stronger supersession proof.
    if "release-cycle-plan.json" in files:
        plan = json.loads(files["release-cycle-plan.json"])
        require(plan.get("repository") == repository and plan.get("prepare") is True
                and plan.get("release") == cycle["release"]
                and plan.get("staging") == cycle["backup_sha"]
                and plan.get("main") == cycle["main"], "Closure receipt and plan disagree")
        if isinstance(plan.get("release_sha"), str) and SHA.fullmatch(plan["release_sha"]):
            result.update(release=plan["release"], release_sha=plan["release_sha"])
    return result


def evidence_time(value):
    """Unknown/naive timestamps cannot establish that a hold has been resolved."""
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp if stamp.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def backup_hold(repository, branch, plan, attempt_info):
    """Keep enough identity and timing to distinguish superseded and active use."""
    if (plan.get("repository") != repository or plan.get("prepare") is not True
            or not isinstance(plan.get("staging"), str) or not SHA.fullmatch(plan["staging"])
            or not isinstance(plan.get("release_sha"), str) or not SHA.fullmatch(plan["release_sha"])
            or not isinstance(plan.get("release"), str)
            or not plan["release"].startswith("release-")
            or branch != f"backup/staging-before-reset-{plan['staging'][:12]}"):
        return None
    return dict(backup=branch, backup_sha=plan["staging"], release=plan["release"],
                release_sha=plan["release_sha"], status=attempt_info.get("status"),
                finished_at=attempt_info.get("updated_at"))


def hold_resolved(hold, cycles):
    if not hold or hold["status"] != "completed":
        return False
    ended = evidence_time(hold["finished_at"])
    if ended is None:
        return False
    for cycle in cycles:
        completed = evidence_time(cycle["completed_at"])
        if (completed is not None and completed > ended
                and all(cycle.get(key) == hold[key]
                        for key in ("backup", "backup_sha", "release", "release_sha"))):
            return True
    return False


def completed_cycles(repository):
    runs = {run["id"]: run for run in paginated(
        f"repos/{repository}/actions/workflows/branch-cleanup.yml/runs")}
    cycles, protected, backup_holds = [], set(), {}
    for run_id in sorted(runs):
        artifacts = paginated(f"repos/{repository}/actions/runs/{run_id}/artifacts")
        for artifact in artifacts:
            match = EVIDENCE_NAME.fullmatch(artifact["name"])
            if not match or artifact["expired"]:
                continue
            attempt = match[1]
            files = read_evidence(repository, artifact)
            # Retain each unresolved use, not just whether *any* run ever failed.
            related = {f"staging-next-cycle-{run_id}-{attempt}"}
            plan = {}
            if "release-cycle-plan.json" in files:
                plan = json.loads(files["release-cycle-plan.json"])
                if plan.get("prepare") and plan.get("staging") and SHA.fullmatch(plan["staging"]):
                    related.add(f"backup/staging-before-reset-{plan['staging'][:12]}")
            if "carryover-manifest.json" in files:
                related.add(json.loads(files["carryover-manifest.json"])["candidate"])
            attempt_info = github("api", f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}")
            try:
                decoded = decode_cycle(repository, run_id, attempt, files, artifact["created_at"])
                independent = (decoded["source"] == "closure-receipt-v2"
                               and attempt_info.get("status") == "completed"
                               and attempt_info.get("conclusion") in ("success", "failure", "cancelled", "timed_out"))
                require(attempt_info["conclusion"] == "success" or independent,
                        "No successful attempt or independent completed closure proof")
                cycles.append(decoded)
            except (RuntimeError, KeyError, ValueError):
                protected.update(related)
                for branch in related:
                    if ref_kind(branch) == "backup":
                        backup_holds.setdefault(branch, []).append(
                            backup_hold(repository, branch, plan, attempt_info))
    # Every use must be superseded; one active, newer, unrelated or unknown use
    # keeps the shared backup. This only lifts a hold: normal cycle/age retention,
    # exact SHA checks, content proof and post-approval revalidation still apply.
    for branch, holds in backup_holds.items():
        if all(hold_resolved(hold, cycles) for hold in holds):
            protected.discard(branch)
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
               keep_cycles=3, min_days=DEFAULT_MIN_DAYS, candidate_states=None,
               scope="all", include_legacy_candidates=False):
    validate_policy(keep_cycles, min_days)
    now = datetime.fromisoformat(assessed_at)
    require(now.tzinfo is not None, "Retention timestamp must include a timezone")
    refs = scoped_refs(refs, scope)
    if candidate_states is None:
        candidate_states = {item["candidate"]: dict(legacy=False, status="completed", conclusion="success")
                            for item in cycles}
    candidate_states = {branch: state for branch, state in candidate_states.items() if branch in refs}
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
        kind = ref_kind(branch)
        evidence = associations.get(branch, [])
        if kind == "candidate":
            state = candidate_states.get(branch, dict(legacy=True, status="unknown"))
            if state.get("open_prs"):
                reason = "Candidate is the source of an open PR"
            elif not state["legacy"] and state["status"] != "completed":
                reason = "Preparation run is still active or waiting; candidate is needed"
            elif state["legacy"] and not include_legacy_candidates:
                reason = "Legacy candidate needs explicit review; enable include_legacy_candidates to assess its content"
            elif state["legacy"] and state.get("active_preparations"):
                reason = "Cannot retire a legacy candidate while a preparation run is active or waiting"
            elif not preserved(sha):
                reason = "Candidate has unresolved or unique changes; retain for use/investigation"
            else:
                reason = ("Reviewed legacy candidate is redundant; all content remains preserved"
                          if state["legacy"] else
                          "Completed preparation is superseded/promoted; all candidate content remains preserved")
                cleanup[branch] = sha
        elif kind != "backup":
            reason = "Unsupported recovery ref name; manual investigation required"
        elif branch in protected:
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
        assessments.append(dict(branch=branch, sha=sha, kind=kind,
                                status="Cleanup candidate" if branch in cleanup else "Keep", reason=reason))
    return dict(schema=2, repository=repository, assessed_at=assessed_at, keep_cycles=keep_cycles,
                min_days=min_days, refs=refs, cycles=cycles, protected=sorted(protected),
                candidate_states=candidate_states, scope=scope, include_legacy_candidates=include_legacy_candidates,
                assessments=assessments, cleanup=cleanup)


def assess(repository, assessed_at, keep_cycles, min_days, scope="all", include_legacy_candidates=False):
    validate_policy(keep_cycles, min_days)
    refs = scoped_refs(recovery_refs(), scope)
    require("main" in refs and "staging" in refs, "Main and staging refs are required")
    cycles, protected = completed_cycles(repository) if scope != "candidates" else ([], set())
    states = candidate_run_states(repository, refs) if scope != "backups" else {}
    return build_plan(repository, refs, cycles, protected, assessed_at,
                      preservation_checker(repository, refs["main"], refs["staging"]), keep_cycles, min_days,
                      states, scope, include_legacy_candidates)


def execute(plan):
    require(plan.get("schema") == 2, "Housekeeping plan format changed; rerun assessment")
    require(plan["repository"] == os.environ["GITHUB_REPOSITORY"], "Retention plan repository mismatch")
    approval_environment(plan["repository"])
    current = assess(plan["repository"], plan["assessed_at"], plan["keep_cycles"], plan["min_days"],
                     plan["scope"], plan["include_legacy_candidates"])
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
                        help="Minimum backup age in days (default: 30; use 3 for demo assessment)")
    parser.add_argument("--scope", choices=("candidates", "backups", "all"), default="candidates")
    parser.add_argument("--include-legacy-candidates", choices=("true", "false"), default="false")
    args = parser.parse_args()
    repository = os.environ["GITHUB_REPOSITORY"]
    if args.action == "execute":
        execute(json.loads(Path(args.plan).read_text()))
        return
    approval_environment(repository)
    plan = assess(repository, datetime.now(timezone.utc).isoformat(), args.keep_cycles, args.min_days,
                  args.scope, args.include_legacy_candidates == "true")
    Path(args.plan).write_text(json.dumps(plan, indent=2) + "\n")
    print(report(plan))


def report(plan):
    assessments = plan["assessments"]
    delete_count = len(plan["cleanup"])
    noun = "branch" if delete_count == 1 else "branches"
    lines = ["## Recovery housekeeping assessment", "",
             f"**{delete_count} {noun} eligible for deletion; {len(assessments) - delete_count} retained.**", "",
             f"Scope: {plan['scope']}. Candidates have no age/cycle retention; active runs, open PRs and unique changes are protected.",
             f"Backup policy: keep {plan['keep_cycles']} successful cycles and at least {plan['min_days']} days."]
    for kind, title in (("candidate", "Temporary candidates"), ("backup", "Backups"), ("other", "Other recovery refs")):
        items = [item for item in assessments if item["kind"] == kind]
        if not items:
            continue
        deleted = [item for item in items if item["branch"] in plan["cleanup"]]
        kept = [item for item in items if item["branch"] not in plan["cleanup"]]
        lines.extend(["", f"### {title}: {len(deleted)} delete, {len(kept)} keep"])
        for item in deleted:
            lines.append(f"- **DELETE** `{item['branch']}` at `{item['sha']}` — {item['reason']}")
        reasons = {}
        for item in kept:
            reasons.setdefault(item["reason"], []).append(item["branch"])
        for reason, branches in reasons.items():
            lines.extend(["", f"Keep — {reason} ({len(branches)}):"])
            lines.extend(f"- `{branch}`" for branch in branches)
    if plan["scope"] == "candidates":
        lines.extend(["", "**Backups are outside this assessment and will not be deleted.**"])
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, ValueError, KeyError) as error:
        sys.exit(f"BLOCKED: {error}")
