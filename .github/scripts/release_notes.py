"""Concise release notes from deployed non-merge commits, not duplicated PR lists."""

import re
import subprocess

from branch_audit import git, is_ancestor
from release_cycle import require


def previous_source_tag(sha, tags, releases):
    published = {release["tag_name"] for release in releases
                 if not release.get("draft") and not release.get("prerelease")}
    eligible = []
    for tag in tags:
        # Complete history of the target is available from checkout. A tag
        # created after checkout can point to a newer, unavailable commit;
        # such a tag cannot be an ancestor of this release.
        if tag["sha"] == sha or subprocess.run(
                ["git", "cat-file", "-e", f"{tag['sha']}^{{commit}}"], capture_output=True).returncode:
            continue
        if is_ancestor(tag["sha"], sha):
            distance = int(git("rev-list", "--count", f"{tag['sha']}..{sha}"))
            eligible.append((distance, tag["name"], tag))
    published_ancestors = [item for item in eligible if item[1] in published]
    choices = published_ancestors or eligible
    require(choices, "No ancestor release tag is available for changelog generation; inspect the baseline")
    # Use source-history proximity, not publication timing or version order:
    # CI can finish out of order for adjacent merges into main.
    return min(choices, key=lambda item: (item[0], item[1]))[2]


def change_titles(base_sha, sha):
    records = subprocess.check_output([
        "git", "log", "--reverse", "--topo-order", "--no-merges", "-z", "--format=%s",
        f"{base_sha}..{sha}", "--"], text=True)
    titles, seen = [], set()
    for subject in records.split("\0"):
        title = re.sub(r"(?:\s+\(#\d+\))+\s*$", "", subject).strip()
        if title and title not in seen:
            titles.append(title)
            seen.add(title)
    return titles


def build_release_notes(repository, sha, release_branch, version, tags, releases):
    base = previous_source_tag(sha, tags, releases)
    titles = change_titles(base["sha"], sha)
    lines = [f"Source release from `{release_branch}` → `main`.",
             f"Commit: `{sha}`. Main CI passed for this SHA. VIP deployment is not asserted by this release.",
             "", "## Changes", ""]
    lines.extend(f"- {title}" for title in titles)
    if not titles:
        lines.append("No new non-merge commit titles in this source range.")
    lines.extend(["", f"**Full changelog:** [{base['name']}…{version}]"
                  f"(https://github.com/{repository}/compare/{base['name']}...{version})"])
    return "\n".join(lines)
