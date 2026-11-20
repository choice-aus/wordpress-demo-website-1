#!/usr/bin/env python3
"""Publish a versioned source release for a verified release-to-main merge."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from branch_audit import git, github, is_ancestor
from release_cycle import require
from release_notes import build_release_notes

VERSION = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
AUTOMATION = "wordpress-source-release-v1"
WORKFLOW_PATH = ".github/workflows/publish-github-release.yml"


def pages(endpoint, collection=None):
    results = []
    page = 1
    while True:
        separator = "&" if "?" in endpoint else "?"
        batch = github("api", f"{endpoint}{separator}per_page=100&page={page}")
        items = batch[collection] if collection else batch
        results.extend(items)
        if len(items) < 100:
            return results
        page += 1


def version_tuple(tag):
    match = VERSION.fullmatch(tag)
    return tuple(map(int, match.groups())) if match else None


def bump_kind(labels):
    selected = {item["name"].lower().removeprefix("release:") for item in labels
                if item["name"].lower() in ("release:patch", "release:minor", "release:major")}
    require(len(selected) <= 1, "Conflicting release bump labels; use at most one of release:patch/minor/major")
    return next(iter(selected), "patch")


def increment(tag, bump):
    major, minor, patch = version_tuple(tag)
    require(bump in ("patch", "minor", "major"), "Unknown version bump")
    if bump == "major":
        return f"v{major + 1}.0.0"
    if bump == "minor":
        return f"v{major}.{minor + 1}.0"
    return f"v{major}.{minor}.{patch + 1}"


def release_pr(repository, sha):
    require(bool(re.fullmatch(r"[0-9a-f]{40}", sha)), "A full immutable merge SHA is required")
    require(is_ancestor(sha, "origin/main"), "Release commit is not in main")
    # Do not backfill old history when the workflow is first introduced or an
    # old CI run is retried. DEV-223's own merge is eligible.
    exists = subprocess.run(["git", "cat-file", "-e", f"{sha}:{WORKFLOW_PATH}"], capture_output=True)
    if exists.returncode:
        return None
    prs = pages(f"repos/{repository}/commits/{sha}/pulls")
    eligible = [pr for pr in prs if pr.get("merged_at") and pr.get("merge_commit_sha") == sha
                and pr.get("base", {}).get("ref") == "main"
                and pr.get("head", {}).get("ref", "").startswith("release-")
                and len(pr["head"]["ref"]) > len("release-")
                and pr["head"].get("repo") and pr["base"].get("repo")
                and pr["head"]["repo"]["full_name"] == repository
                and pr["base"]["repo"]["full_name"] == repository]
    if not eligible:
        return None
    require(len(eligible) == 1, "Multiple release PRs match this merge; manual review required")
    require(len(git("rev-list", "--parents", "-n", "1", sha).split()) == 3,
            "Release-to-main promotion must use a two-parent merge commit")
    runs = pages(f"repos/{repository}/actions/workflows/ci.yml/runs?head_sha={sha}&event=push&status=success",
                 "workflow_runs")
    require(any(run.get("head_sha") == sha and run.get("head_branch") == "main"
                and run.get("event") == "push" and run.get("conclusion") == "success" for run in runs),
            "Successful main push CI is required for the exact release SHA")
    return eligible[0]


def tag_info(repository, ref):
    obj = ref["object"]
    metadata = None
    for _ in range(10):
        if obj["type"] == "commit":
            return dict(name=ref["ref"].removeprefix("refs/tags/"), sha=obj["sha"], metadata=metadata)
        require(obj["type"] == "tag", "Version tag does not resolve to a commit")
        tag = github("api", f"repos/{repository}/git/tags/{obj['sha']}")
        if metadata is None:
            try:
                message = json.loads(tag.get("message", ""))
                if isinstance(message, dict) and message.get("automation") == AUTOMATION:
                    metadata = message
            except ValueError:
                pass
        obj = tag["object"]
    raise RuntimeError("Version tag chain is too deep")


def version_tags(repository):
    refs = github("api", f"repos/{repository}/git/matching-refs/tags/v")
    return [tag_info(repository, ref) for ref in refs
            if version_tuple(ref["ref"].removeprefix("refs/tags/")) is not None]


def reserve_version(repository, sha, pr, bump, occupied_versions=()):
    """Atomic create-only tag refs allocate unique versions across parallel merges."""
    for _ in range(5):
        tags = version_tags(repository)
        require(tags, "No vMAJOR.MINOR.PATCH baseline tag exists; establish the initial version first")
        for tag in tags:
            metadata = tag["metadata"] or {}
            if metadata.get("repository") == repository and metadata.get("merge_sha") == sha:
                require(tag["sha"] == sha and metadata.get("pr") == pr["number"]
                        and metadata.get("version") == tag["name"], "Reserved release tag metadata mismatch")
                return tag  # Resume publication after a prior run reserved the tag.
        previous = max(tags, key=lambda item: version_tuple(item["name"]))
        version = increment(previous["name"], bump)
        require(version not in occupied_versions,
                "The next version already belongs to a release/draft without a matching tag; review it first")
        metadata = dict(automation=AUTOMATION, repository=repository, merge_sha=sha,
                        pr=pr["number"], version=version, bump=bump, previous_tag=previous["name"])
        obj = github("api", f"repos/{repository}/git/tags", "--method", "POST",
                     "-f", f"tag={version}", "-f", f"message={json.dumps(metadata, sort_keys=True)}",
                     "-f", f"object={sha}", "-f", "type=commit")
        try:
            github("api", f"repos/{repository}/git/refs", "--method", "POST",
                   "-f", f"ref=refs/tags/{version}", "-f", f"sha={obj['sha']}")
            return dict(name=version, sha=sha, metadata=metadata)
        except subprocess.CalledProcessError:
            # Retry only if a real competing ref exists. Network/auth failures
            # must not silently allocate another version.
            matches = github("api", f"repos/{repository}/git/matching-refs/tags/{version}")
            require(any(ref["ref"] == f"refs/tags/{version}" for ref in matches),
                    "Tag reservation failed without a competing ref; check access/network")
    raise RuntimeError("Repeated tag allocation races; rerun this publication")


def publish(repository, sha):
    pr = release_pr(repository, sha)
    if pr is None:
        return dict(status="skipped", sha=sha, reason="Not a new eligible release-to-main merge")
    releases = pages(f"repos/{repository}/releases")
    tags = version_tags(repository)
    tag_by_name = {tag["name"]: tag for tag in tags}
    for release in releases:
        tag = tag_by_name.get(release["tag_name"])
        if tag and tag["sha"] == sha:
            require(not release["draft"], "A draft release already exists for this SHA; review it before publishing")
            return dict(status="already-published", sha=sha, version=tag["name"], url=release["html_url"])
    bump = bump_kind(pr.get("labels", []))
    tag = reserve_version(repository, sha, pr, bump, {release["tag_name"] for release in releases})
    # Never trust a tag name without checking its immutable target before publishing.
    refs = github("api", f"repos/{repository}/git/matching-refs/tags/{tag['name']}")
    exact = [ref for ref in refs if ref["ref"] == f"refs/tags/{tag['name']}"]
    require(len(exact) == 1 and tag_info(repository, exact[0])["sha"] == sha,
            "Reserved tag changed before publication; no release published")
    body = build_release_notes(repository, sha, pr["head"]["ref"], tag["name"], tags, releases)
    current_main = github("api", f"repos/{repository}/git/ref/heads/main")["object"]["sha"]
    # CI for an older merge can finish after a newer merge's CI. Publish its
    # exact source snapshot, but never deliberately promote it over current main.
    make_latest = "true" if current_main == sha else "false"
    result = github("api", f"repos/{repository}/releases", "--method", "POST",
                    "-f", f"tag_name={tag['name']}", "-f", f"target_commitish={sha}",
                    "-f", f"name={tag['name']}", "-f", f"body={body}",
                    "-F", "draft=false", "-F", "prerelease=false", "-f", f"make_latest={make_latest}")
    return dict(status="published", sha=sha, version=tag["name"], url=result["html_url"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("merge_sha")
    args = parser.parse_args()
    require(os.environ.get("GITHUB_REF") == "refs/heads/main", "Run release publication from main")
    result = publish(os.environ["GITHUB_REPOSITORY"], args.merge_sha)
    Path("github-release-result.json").write_text(json.dumps(result, indent=2) + "\n")
    report = json.dumps(result, indent=2)
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(f"## GitHub source release\n\n```json\n{report}\n```\n")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(f"BLOCKED: {error}")
