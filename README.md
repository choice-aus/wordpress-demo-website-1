# wordpress-demo-website-1

### Startup flow for this template ###

```bash
composer install
docker compose build
docker compose up -d
```

### Restart cleanly: ### 
```bash
docker compose down
docker compose up -d --build
```

### Sanity Checklist ###

After everything is up:

Visit:
👉 https://mysite1.test:4441/

phpMyAdmin:
👉 http://localhost:8082

Mailhog:
👉 http://localhost:8025

---

## Git Branching Strategy

### Branches

| Branch | Purpose |
|---|---|
| `main` | Trusted, production-ready code |
| `develop` | Sandbox for experimentation and early integration testing |
| `staging` | Shared sprint integration and testing |
| `release-*` | Temporary release branches for approved features |
| `feature/*` | Individual development tasks |
| `fix/*`, `hotfix/*`, `chore/*` | Fixes, urgent fixes and maintenance using the same promotion flow |

### Promotion workflow

1. Create `feature/*`, `fix/*`, `hotfix/*` or `chore/*` branches from `main`.
2. Merge these work branches into `staging` as work progresses. Pull requests are optional.
3. Once a change is approved, create a pull request from its work branch into the current `release-*` branch.
4. Use **Squash and merge** when merging work branches into a release branch.
5. Test the completed release.
6. Create a pull request from `release-*` into `main`.
7. Use **Create a merge commit** when promoting a release into `main`.

### Promotion restrictions

- Only `feature/*`, `fix/*`, `hotfix/*` and `chore/*` branches may merge into `release-*`.
- Only `release-*` branches may merge into `main`.
- `staging` must never be promoted directly into `release-*` or `main`.
- Failed or unapproved features must not be included in a release.

### Merge methods

| Destination | Merge method |
|---|---|
| `staging` | Merge commit (PR optional) |
| `release-*` | Squash and merge |
| `main` | Merge commit |

## Creating a release branch independently

Run **Create release branch** from `main` and provide its single input,
`release_branch` (for example `release-dev-224`). The workflow creates that name
from the exact current main SHA and records the result in its summary and
`release-branch-creation.json` artifact. It does not move an existing branch,
publish a GitHub Release, reset staging, clean up refs or change VIP settings.
It shares the closure/housekeeping concurrency lock. A changed main SHA or a
concurrently created name stops creation; a retry never overwrites existing work.
`release-dev-223` already exists for DEV-223 and does not need to be recreated.

If you use this standalone workflow to pre-create the next release, leave
`next_release` blank when running **Branch cleanup**. That input is for creating
a new branch after closure; it intentionally rejects an already existing name.
Branches created with `GITHUB_TOKEN` do not automatically start other Actions
workflows through ordinary push/create events. This workflow does not claim a
deployment; VIP branch switching/deployment is a separate operation.

## Automated GitHub source releases

**Publish GitHub source release** runs after successful `CI` for a push to
`main`. It accepts only a verified, two-parent `release-*` PR merge into main,
checks CI for that exact merge SHA, and tags that SHA even if main has advanced.
Feature/PR CI, failed main CI and ordinary direct pushes cannot publish a release.
History before this workflow was introduced is not automatically backfilled.

Stable tags follow the existing `v1.0.0` convention. Each new release increments
patch by default (`v1.0.0` → `v1.0.1`). On the **release-to-main PR**, optionally
use one of `release:minor`, `release:major` or `release:patch`. Minor resets patch;
major resets minor and patch. Conflicting bump labels stop publication. Labels
on individual feature PRs do not select the release version.

The workflow creates annotated tags with PR/SHA/version metadata and concise
release notes. Notes list deployed non-merge commit titles since the nearest
published stable ancestor tag, falling back to an existing stable ancestor tag
when no published baseline exists. This lists each normal feature squash once,
without repeating its release-to-main promotion PR. Entries preserve ticket IDs
and descriptions, omit authors/PR attribution, and strip trailing GitHub `(#123)`
suffixes. Identical cleaned titles are deduplicated; different changes sharing
a ticket stay separate. The release branch, immutable SHA and one full-changelog
link retain release-level context. Out-of-order CI never selects a newer,
non-ancestor tag as the notes baseline. Existing published notes, including
`v1.0.1`, are not rewritten; the new format applies to future publications.
Existing tags and published releases are never moved or edited.
A successful retry is a no-op; a failure after tag reservation can resume that
same version. Different merge SHAs use separate concurrency groups and atomic
create-only tag allocation with collision retries, so pending releases are not
silently displaced by a global single-pending-run queue. Versions are allocated
in reservation order; CI completion/publication order can differ from merge order.
Publication of an older SHA does not deliberately replace the latest release
when main has advanced. GitHub refs and Releases are separate API operations;
failures can leave a reserved tag awaiting publication. Never repoint that tag
to recover from a failure.

For an explicit retry, run the workflow from `main` with `merge_sha` set to the
original full release-to-main merge SHA; all eligibility and CI checks still
apply. No typing of a version is required. The existing `v1.0.0` release is
preserved, and the first new patch version is based on the highest stable
SemVer tag present when allocation runs. Tags created by this workflow must
remain immutable by team policy and the eventual tag-protection configuration.

This is independent of **Branch cleanup**: old-release cleanup and reruns must
not create retrospective releases or control whether publication happens.
The current deploy workflow only prints `Would deploy`; it is not a real VIP
deployment confirmation. Therefore these are explicitly **source releases**,
not assertions that production has deployed. If publication should mean
"deployed to production", later gate this same publisher on a verified VIP
deployment of the exact source/build SHA. GitHub supports merged-PR and
deployment-status events, but the real VIP signal still needs to be selected.
See [GitHub workflow events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
and [GitHub Releases and tags](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases).

Merging DEV-223 into main enables this automatic publishing path after its main
push CI passes. The first live tag/release publication still needs operational
validation; local tests mock GitHub writes. `GITHUB_TOKEN` release/tag writes do
not themselves start ordinary downstream Actions event workflows, so future
deployment chaining must be explicit rather than relying on that side effect.

## VIP deployment branches and release environments

VIP Default Deployment tracks a specific branch in the application's configured
wpcomvip repository. Updating that configured branch in the Dashboard requires
the branch to exist, and **Change Branch and Deploy** starts a deployment.
See [VIP deploying-branch updates](https://docs.wpvip.com/code-deployment/default-deployment/deploying-branches/update/).

For an optional VIP environment that tracks a rotating `release-*` branch, use
this sequence once the old release has reached main:

1. Create the next release branch from main with **Create release branch**.
2. In the VIP Dashboard, point that environment to the new existing branch.
3. Confirm its deployment succeeds and the old branch is no longer configured.
4. Run **Branch cleanup** for the old release, leaving `next_release` blank.

Do not rely on unspecified VIP behavior after deleting a configured deploying
branch. This repo does not read or change VIP environment mappings, so the
operator must verify them before approving deletion. Creating the next branch
only inside the cleanup workflow is too late for this handoff: cleanup deletes
the old release before its optional next-branch creation step.

Production and shared UAT can retain stable `main` and `staging` mappings, if
those are the branches configured for the actual VIP application. A hosted
release environment is optional and remains a team cost/configuration decision.
If none is hosted, the branch workflow still prepares the next release and no
release-environment handoff is needed. If a release slot is hosted, also consider
a stable deployment branch for it, with explicit controlled promotion, instead
of repeatedly rebinding the environment to disposable branches.

Before applying this plan on VIP, confirm the actual source repository (this
demo repository versus a wpcomvip mirror), environment mappings and whether a
CI/CD-built branch is used. VIP reserves the `*-built` suffix for build/deploy
flows; source release branches and deployment branches need not be identical.
See [VIP build and deploy](https://docs.wpvip.com/code-deployment/default-deployment/build-and-deploy/).
No VIP integration or automatic mapping validation is enabled by DEV-223.

## UAT Demonstration

This change is used to demonstrate the handling of a feature that fails UAT and must be carried forward into a future staging cycle.


## Closing a release cycle

Run **Branch cleanup** after the release cycle is closed. The workflow records
main, staging, the selected release and eligible cleanup branch SHAs in an
assessment artifact. Review that snapshot before approving the `branch-cleanup`
environment, which must have at least one required reviewer configured. All
operations stop if that environment rule cannot be verified.
The selected release must itself be cleanup eligible, with no open source PRs
or protected PR labels, before assessment or any writes can proceed. This also
applies to preparation-only runs.

- `confirm_closed=false` performs assessment only.
- `confirm_closed=true`, `preparation_only=true` prepares and verifies a candidate
  after environment approval. It creates a backup/candidate and publishes
  successful carryovers, but never resets staging or deletes branches.
- `confirm_closed=true`, `preparation_only=false` prepares, verifies, resets staging
  to the verified candidate and deletes eligible feature/release branches after
  environment approval. Each work branch must pass exact combined-diff or
  clean reconstructed-tree verification against its squash in main; the release
  must have reached main through a merge commit. The reconstruction handles
  changes already released separately, including the DEV-215 historical case.
- `audit_mode=specific` selecting an older completed release allows verified branch
  cleanup only. It never prepares a candidate or resets staging. Preparation-only
  mode on an older release performs no writes.
- `next_release=release-<new-name>` optionally creates the next release from the
  assessed main SHA after staging promotion and branch cleanup succeed. The name
  must be new; an existing branch is never moved or adopted. Leave blank to skip.
- `remove_candidate=true` (the default) deletes this run's temporary candidate
  immediately after staging promotion verifies, using its exact SHA lease.
  Staging retains the verified commit and the original backup remains. Candidate
  removal happens before feature/release cleanup, so failure of that later task
  does not leave a redundant candidate behind. Set `false` only to retain it
  deliberately for troubleshooting.
  Preparation-only runs never create the next release or remove a candidate.
  Older-release cleanup has no candidate to remove and disables this option;
  requests to create a next release in that mode are rejected.

The assessment lists **all assessed branches**, including excluded branches
and their reasons. A missing/deleted selected release gets a specific error.

The **Prepare next UAT cycle** entry point delegates to the same workflow with
preparation-only enabled, including the same approval and concurrency controls.
Use the reviewed workflow revision when dispatching; use main once these changes
have been reviewed and promoted there.

Carryovers use the exact revisions recorded in labelled, merged staging PRs,
excluding subsequent feature-branch changes. Successful merges are preserved
when another carryover fails. Every failed carryover is reported in the job
summary and `carryover-manifest.json`; any failure or unexplained staging commit
blocks staging reset and cleanup. Merge resolutions in staging must also pass
Git's reconstructed-merge check. Unlabelled unreleased changes block reset.
Historical `carryover-next-cycle` and `uat-failed-retest` labels on merged staging
PRs no longer prevent cleanup once a later, verified release covers their
reviewed revisions and the current work branch tip. The labels are not removed:
their history is retained. Labels on release PRs, later staging revisions,
`deferred`, `rejected` and open PRs continue to protect branches. Already deployed
labelled revisions are excluded from carryover without depending on the source
branch still existing. Unreleased revisions still require preservation.

Backups use `backup/staging-before-reset-<original-SHA-prefix>` and are never
overwritten or deleted by **Branch cleanup**. Candidates use
`staging-next-cycle-<run-id>-<attempt>` so a rerun preserves previous preparation
results. A successful staging promotion writes `promotion-receipt.json` before
release cleanup and records candidate removal there, even if a later step fails.
A successful full latest-release closure also writes `closure-receipt.json`.
Both receipts are uploaded as evidence, so the temporary branch is not needed
for reporting. Assessment-only runs create neither candidates nor backups.
Existing refs require a separate housekeeping assessment; merely deploying
these scripts does not prune them.
When retrying failed jobs, execution uses the successful assessment's artifact
name and revalidates its original snapshot. Re-running all jobs creates a new
assessment artifact for that attempt. If the snapshot is stale, start a new
assessment and review it before approving execution.

Release eligibility and branch SHAs are revalidated after approval and before
writes. Staging updates use an explicit SHA lease; cleanup uses one atomic push
with a lease for each branch. Concurrent changes to write targets reject the
push. GitHub protection rules can also reject writes; the workflow does not
bypass them. Main, PR metadata and unrelated refs are re-read before writes, but
Git offers no atomic transaction covering API metadata and unrelated refs: avoid
merging a new release or changing carryover labels during closure. If eligibility
changes after staging promotion, release cleanup stops. Staging retains the
verified commit and the backup retains original staging; the workflow does not
attempt a rollback. If candidate removal fails, execution stops before release
cleanup. If next-release creation fails after cleanup, earlier successful
operations remain in place; inspect the evidence before
retrying. Do not recreate the deleted completed release merely to rerun it.

## Temporary candidates and backup retention

Run **Recovery branch housekeeping** from the reviewed workflow revision,
using main after promotion. Start with `confirm_delete=false` to assess only.
Select `scope=candidates` (the default), `scope=backups`, or `scope=all`.
It shares the closure concurrency lock and never resets staging or deletes
work/release branches. Review `retention-plan.json` and the reasons in the
summary. With `confirm_delete=true`, the `branch-cleanup` environment approval
is required, and all refs, run evidence and content preservation are revalidated
before one atomic push with a lease for every deletion target.

Temporary `staging-next-cycle-*` branches have **no cycle-count or age retention**.
For a completed originating run, a candidate is eligible once all its content is
accounted for in current main/staging or verified releases. This includes a
failed/preparation-only candidate that has subsequently been safely superseded.
Active/waiting runs, open source PRs and candidates with unique or unresolved
changes are kept. Candidate-only assessment reads run metadata and content
proof; it does not download expiring backup artifacts.

For the one-time legacy tidy-up, select `scope=candidates`,
`include_legacy_candidates=true`, and `confirm_delete=false`. This explicitly
reviews old names such as `staging-next-cycle` or candidates whose run can no
longer be identified. They still need content-preservation proof, and are kept
if a known preparation is active or waiting. Review the proposed deletions, then
start a new run with the same scope/legacy option and `confirm_delete=true` to
request approved deletion. This scope cannot delete backup branches. Candidates
containing unique work require investigation/preservation or explicit manual
abandonment; this workflow does not discard their unique content.

**Backups only:** the default policy retains the latest **3 verified successful
cycles** and everything younger than **30 days**. The `retention_days` input
defaults to `30`; for this fast-changing demo, select `3` without editing code.
It must be a positive whole number. Backups of the latest three successful cycles
are always kept, regardless of age. Both thresholds must be passed before older
backups are eligible for deletion. The chosen
value is stored in the assessment and reused during post-approval revalidation.
Retention is not automatic after every release; it is a separate assessed action.

For a backup demo preview, select `scope=backups`, `retention_days=3`
and `confirm_delete=false`. Review the listed reasons, then use the same age
with `confirm_delete=true` for an approved deletion run. Locally, the equivalent
read-only assessment is `python3 .github/scripts/recovery_retention.py assess --scope backups --min-days 3`
with `GITHUB_REPOSITORY` and GitHub authentication configured. Omitting
`--min-days` uses the script's `DEFAULT_MIN_DAYS` value of 30.

Backup eligibility requires a successful full-closure run attempt and its matching
receipt, or equivalent DEV-219/220 plan, manifest and full-success result artifacts.
Each ref must still have the recorded SHA, and its content must be accounted
for in current main/staging or verified release evidence. Failed, preparation-only,
active or unverifiable backups are retained. If a backup is shared with a
failed/preparation-only run, it is retained too. Missing/expired artifacts and
legacy names without sufficient proof produce **Keep**, not deletion permission.
Network/authentication errors block assessment rather than being treated as
missing proof. Artifact retention therefore affects how much historical cleanup
can be verified; unknown legacy backups need separate manual investigation.
These backup safeguards do not permanently protect redundant temporary candidates.

The summary leads with deletion/retention counts, separates temporary candidates
from backups, lists exact deletion SHAs, and groups retained branches by reason.
Scope, legacy-review permission, run states and all relevant refs are recorded
in the plan and revalidated after approval. Changes require a fresh assessment.

Suggested DEV-222 rollout: review the candidate-only legacy inventory and run
the local tests, then promote through staging and release to main. Start with a
candidate-only housekeeping assessment; after review, request deletion separately.
Close the next release with an explicit new release name and default candidate removal.
Verify main/staging and retained backup SHAs. Remote executions and deletions
require explicit authorisation.

Run the release safety tests locally with:

```sh
python3 -m unittest discover -s tests -v
```

The tests use temporary Git repositories, mock GitHub writes, and exercise real
push hooks and atomic leases only against local bare repositories. They do not
modify GitHub branches. Preparation-only Actions runs do write candidate and backup
branches, so running that mode still requires approval to publish those refs.

## Local commit checks

Run this once in every checkout (including a separate WSL or Windows checkout):

```sh
sh scripts/install-git-hooks.sh
```

Python 3 and Git are required. The installer sets this checkout's
`core.hooksPath` and refuses to replace a different existing hooks path.
The `commit-msg` hook rejects titles outside `[ABC-123] - Description` before
the commit is created. A `pre-commit` hook cannot validate the final message.
The `pre-push` hook checks outgoing branch commits, including commits created
by tools that skip the message hook. Fetch the remote first if its base
objects are unavailable. First pushes of new branches exclude existing
`origin/main` history. Git-generated merge commits are exempt; squash commits
and PR titles must follow the title rule.

Actions runs the same validator as the separate **Commit title check** on
pushes and PRs into staging, release branches and main. Local hooks can be
skipped, so add **Commit title check** as a required status check in those
rulesets after the new check has run. This is a separate GitHub settings change;
adding the workflow alone does not make the check required. Retain
**Validate promotion path** and the other existing required checks. Commit
titles are also still checked inside **Validate promotion path** during rollout,
so existing required-check enforcement is preserved.

## Protected cleanup credentials (DEV-225)

Branch cleanup and Recovery branch housekeeping support an approved automation
user's repository-scoped token, without installing a GitHub App. Separate active
rulesets grant that user only staging force-push and release-deletion exceptions;
normal main/release PR checks and staging deletion protection retain no bypass.

See [Keep rulesets active: setup and live validation](docs/keep-rulesets-active.md).
The default remains the built-in token until the account, environment secret,
main-only approval environment, rulesets and repository variables are configured.
Automation-token mode fails closed on missing credentials or unsafe settings.
The implementation does not change live GitHub settings automatically.

## Outstanding backlog

Completed DEV-220–222 implementation items have been removed from this backlog;
their operating instructions remain above. New ticket branches use
`feature/DEV-123` (or an approved work-branch prefix) and start from current main.

- **Refine backup retirement.** Keep the latest three successful cycles and all
  backups younger than the selected duration. Treat active use, unique content
  and changed SHAs as deletion safeguards. A resolved historical failure should
  not protect a backup forever. Add an explicitly reviewed legacy-adoption path
  for missing/expired provenance and record reliable creation/use timestamps;
  Git commit dates are not backup-creation dates. Preserve approval, content
  proof and exact SHA leases. The three early demo backups were manually
  reviewed and removed; the general policy refinement is not implemented here.
- **Validate release automation rollout (DEV-223).** Test the new main-CI
  publisher, patch/minor/major labels, duplicate/retry behavior and immutable
  tag targets in Actions. Validate standalone branch creation and rejection of
  an existing name. Keep GitHub source publication separate from housekeeping.
- **Choose the VIP deployment model.** Decide whether to fund/configure a hosted
  release slot. Confirm source/build branch mappings and select a production
  deployment-completion signal before making published releases mean deployed
  releases. Establish a reliable guard or operating control against deleting
  any branch still configured on VIP; today that verification is manual.
- **Validate protected cleanup rollout (DEV-225).** Confirm VIP/organization
  approval for the dedicated automation user and token, apply the reviewed
  ruleset/environment configuration, and test rejected writes as well as
  permitted closure/housekeeping operations. The support is implemented;
  account setup and live enforcement remain to be validated. Follow the
  [rollout checklist](docs/keep-rulesets-active.md#validation-and-rollout).
- **Check real-environment rollout.** Verify required checks on the intended VIP
  repository and install local hooks in each developer checkout. The validator
  implementation is complete; account/ruleset configuration is a separate task.
