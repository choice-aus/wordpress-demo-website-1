# wordpress-demo-website-1

This disposable WordPress repository demonstrates a release branching strategy,
shared UAT, unreleased-work carryover, GitHub source releases and guarded release
closure. The deployment workflow is a placeholder; no VIP deployment or branch
mapping integration is enabled.

## Local startup

Use Docker with Compose, plus Composer and a compatible PHP environment
(`composer.json` requires PHP 8.2+ and `mysqli`). Before starting:

- Configure the ignored `.env` with the database credentials and port variables
  referenced by [docker-compose.yml](docker-compose.yml).
- Resolve `mysite1.test` to your local Docker host, normally `127.0.0.1`.
- Supply local TLS files `certs/mysite1.test.pem` and
  `certs/mysite1.test-key.pem`, matching [the Nginx configuration](docker/nginx/default.conf).

```bash
composer install
docker compose build
docker compose up -d
```

Restart the containers while preserving the database volume:

```bash
docker compose down
docker compose up -d --build
```

With this checkout's current `.env` ports, use:

| Service | URL |
| --- | --- |
| WordPress | <https://mysite1.test:4441/> |
| phpMyAdmin | <http://localhost:8082> |
| MailHog | <http://localhost:8025> |

Use the corresponding configured ports if your local `.env` differs.

## Git Branching Strategy

### Branches

| Branch | Purpose |
|---|---|
| `main` | Trusted, production-ready code |
| `develop` | Sandbox for experimentation and early integration testing |
| `staging` | Shared UAT for work being tested before release |
| `release-*` | Temporary release branches for approved features |
| `feature/*` | Individual development tasks |
| `fix/*`, `hotfix/*`, `chore/*` | Fixes, urgent fixes and maintenance using the same promotion flow |

### Promotion workflow

Name ticket branches `<type>/DEV-123`, for example `feature/DEV-123` or
`chore/DEV-227`. All four work-branch prefixes below follow the same path;
`hotfix/*` does not have a direct-to-main exception.

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
| :--- | :--- |
| `staging` | Merge commit (PR optional) |
| `release-*` | Squash and merge |
| `main` | Merge commit |

## Creating a release branch independently

### Workflow: Create release branch

Run from **main**.

| Parameter | Value |
| :--- | :--- |
| `release_branch` | New, unused branch name, for example `release-dev-228` |

The workflow creates that name from the exact current main SHA and records the result in its summary and
`release-branch-creation.json` artifact. It does not move an existing branch,
publish a GitHub Release, reset staging, clean up refs or change VIP settings.
It shares the closure/housekeeping concurrency lock. A changed main SHA or a
concurrently created name stops creation; a retry never overwrites existing work.

Both this workflow and optional next-release creation during closure wait up to
10 minutes for same-SHA push CI to settle, including checks triggered by PAT
pushes to staging/candidates. The latest push CI must succeed, no same-SHA push
CI may remain active, and a short stable interval allows delayed events to appear.
Main and target absence are rechecked while waiting and immediately before the
create-only request. Closure also rechecks staging, backup and approval state.
An HTTP 422 permits one further create-only attempt only when new active CI is
positively observed; the same total time bound applies. Other failures stop
creation. Required-check rules remain enabled and GitHub makes the final decision.

If you use this standalone workflow to pre-create the next release, leave
`next_release` blank when running **Close release cycle**. That input is for creating
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

### Workflow: Publish GitHub source release

Publication normally starts automatically after successful main CI. For an
explicit retry, run from **main** with:

| Parameter | Value |
| :--- | :--- |
| `merge_sha` | Original full, 40-character release-to-main merge SHA |

All eligibility and CI checks still apply. Retrying an already published SHA
returns `already-published` without creating another version.
No typing of a version is required. The existing `v1.0.0` release is
preserved, and the first new patch version is based on the highest stable
SemVer tag present when allocation runs. Tags created by this workflow must
remain immutable by team policy and the eventual tag-protection configuration.

This is independent of **Close release cycle**: old-release cleanup and reruns must
not create retrospective releases or control whether publication happens.
The current deploy workflow only prints `Would deploy`; it is not a real VIP
deployment confirmation. Therefore these are explicitly **source releases**,
not assertions that production has deployed. If publication should mean
"deployed to production", later gate this same publisher on a verified VIP
deployment of the exact source/build SHA. GitHub supports merged-PR and
deployment-status events, but the real VIP signal still needs to be selected.
See [GitHub workflow events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
and [GitHub Releases and tags](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases).

Automatic patch publication and an `already-published` retry are verified through
[`v1.0.4`](https://github.com/choice-aus/wordpress-demo-website-1/releases/tag/v1.0.4).
Minor/major label overrides and mismatched-tag rejection have automated coverage;
their live publication tests are explicitly deferred for this demo.
`GITHUB_TOKEN` release/tag writes do
not themselves start ordinary downstream Actions event workflows, so future
deployment chaining must be explicit rather than relying on that side effect.

## VIP deployment branches and release environments

This section is planning guidance for team adoption after the demo, outside this
repository's implementation scope. Confirm actual platform configuration before
using it; the demo does not interact with VIP.

VIP Default Deployment tracks a specific branch in the application's configured
wpcomvip repository. Updating that configured branch in the Dashboard requires
the branch to exist, and **Change Branch and Deploy** starts a deployment.
See [VIP deploying-branch updates](https://docs.wpvip.com/code-deployment/default-deployment/deploying-branches/update/).

For an optional VIP environment that tracks a rotating `release-*` branch, use
this sequence once the old release has reached main:

1. Create the next release branch from main with **Create release branch**.
2. In the VIP Dashboard, point that environment to the new existing branch.
3. Confirm its deployment succeeds and the old branch is no longer configured.
4. Run **Close release cycle** for the old release, leaving `next_release` blank.

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
No VIP integration or automatic mapping validation is enabled in this repository.

## UAT Demonstration

The DEV-212/DEV-218 cycles demonstrated keeping an unreleased feature while
removing a deployed feature's squashed commits from the next UAT cycle. DEV-212
was subsequently released with DEV-219; it is historical test evidence, not
currently pending carryover. Closure also supports cycles with no carryovers,
where verified staging becomes identical to main.

## Closing a release cycle

Run **Close release cycle** after the release cycle is closed. The workflow records
main, staging, the selected release and eligible cleanup branch SHAs in an
assessment artifact. Review that snapshot before approving the `branch-cleanup`
environment, which must have at least one required reviewer configured. All
operations stop if that environment rule cannot be verified.
The selected release must itself be cleanup eligible, with no open source PRs
or protected PR labels, before assessment or any writes can proceed. This also
applies to preparation-only runs.

### Workflow: Close release cycle

Run from **main**. For checkbox inputs, `true` means checked and `false` means
unchecked. Choose the table for the operation you want.

#### Assessment only

| Parameter | Value |
| :--- | :--- |
| `audit_mode` | `latest` |
| `completed_release` | Leave blank |
| `confirm_closed` | `false` |
| `preparation_only` | `true` |
| `next_release` | Leave blank |
| `remove_candidate` | `true` |

Records the plan without creating branches, resetting staging or deleting refs.
Only assessment runs because `confirm_closed` is unchecked; candidate removal
does not apply.

#### Prepare and verify without resetting staging

| Parameter | Value |
| :--- | :--- |
| `audit_mode` | `latest` |
| `completed_release` | Leave blank |
| `confirm_closed` | `true` |
| `preparation_only` | `true` |
| `next_release` | Leave blank |
| `remove_candidate` | `true` |

After environment approval, creates a backup/candidate and publishes successful
carryovers. Staging and release/work branches remain unchanged. Preparation-only
mode always retains its candidate, regardless of `remove_candidate`.

#### Close the latest release and reset staging

| Parameter | Value |
| :--- | :--- |
| `audit_mode` | `latest` |
| `completed_release` | Leave blank |
| `confirm_closed` | `true` |
| `preparation_only` | `false` |
| `next_release` | Leave blank |
| `remove_candidate` | `true` |

After assessment and environment approval, prepares and verifies carryovers,
resets staging to the verified candidate, removes that candidate and deletes
eligible work/release branches. The original staging backup remains.

For optional follow-up behavior, change the corresponding value in that table:

| Parameter | Optional value |
| :--- | :--- |
| `next_release` | New, unused `release-*` name to create from assessed main after closure |
| `remove_candidate` | `false` to retain this run's candidate for troubleshooting |

An existing next-release branch is never moved or adopted. Leave `next_release`
blank if you already created it with **Create release branch**. Candidate removal
uses its exact SHA lease immediately after verified staging promotion, before
work/release cleanup; failure of that later cleanup does not leave the candidate
behind.

#### Clean up an older completed release only

| Parameter | Value |
| :--- | :--- |
| `audit_mode` | `specific` |
| `completed_release` | Existing completed `release-*` branch older than the latest release |
| `confirm_closed` | `true` |
| `preparation_only` | `false` |
| `next_release` | Leave blank |
| `remove_candidate` | `false` |

Performs verified branch cleanup after approval. An older release cannot prepare
a candidate, reset staging or create a next release. To assess it without writes,
use the same values with `confirm_closed=false`; preparation-only mode on an
older release also performs no writes.

Each work branch must pass exact combined-diff or clean reconstructed-tree
verification against its squash in main; the release must have reached main
through a merge commit. The reconstruction handles changes already released
separately, including the DEV-215 historical case.

The assessment lists **all assessed branches**, including excluded branches
and their reasons. A missing/deleted selected release gets a specific error.

Dispatch operational workflows from **main**. Automation-token mode requires that
branch, and the `branch-cleanup` approval environment must allow exactly branch
`main`, with no tags or wildcard branch policies.

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
overwritten or deleted by **Close release cycle**. Candidates use
`staging-next-cycle-<run-id>-<attempt>` so a rerun preserves previous preparation
results. A successful staging promotion writes `promotion-receipt.json` before
release cleanup and records candidate removal there, even if a later step fails.
A successful full latest-release closure writes a schema-2 `closure-receipt.json`
**before** optional next-release setup. It records the approved deleted branch
SHAs and completed staging promotion. A separate `next-release-result.json`
records `not-requested`, `pending`, `created` or `failed` for that follow-up.
The receipt remains valid if optional setup fails or times out; it never claims
that a next release was created. Both files are uploaded with the other evidence.
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
push. With automation-token mode configured, the approved account bypasses only
the staging force-push and release-deletion guards. Main/release PR checks,
staging deletion protection and other matching rules remain enforced; cleanup
does not disable rulesets. GitHub can still reject a write. Main, PR metadata
and unrelated refs are re-read before writes, but
Git offers no atomic transaction covering API metadata and unrelated refs: avoid
merging a new release or changing carryover labels during closure. If eligibility
changes after staging promotion, release cleanup stops. Staging retains the
verified commit and the backup retains original staging; the workflow does not
attempt a rollback. If candidate removal fails, execution stops before release
cleanup. If next-release creation fails after cleanup, the job still reports a
failure for that follow-up, while the closure receipt preserves the completed
operations. Resolve the CI/protection/name issue and run **Create release branch**
from main with the intended unused branch name. Do not rerun closure or recreate
the deleted completed release merely to recover next-release setup.

## Temporary candidates and backup retention

### Workflow: Clean up recovery branches

Run from **main**. The following tables assess and clean both supported recovery
branch types. The normal backup minimum age is 30 days.

#### Assess candidates and backups

| Parameter | Value |
| :--- | :--- |
| `scope` | `all` |
| `include_legacy_candidates` | `false` |
| `retention_days` | `30` |
| `confirm_delete` | `false` |

Creates an assessment without deleting branches. For a short demo policy, use
`3` days; the DEV-226 closeout used `1`. Keep that chosen age for the deletion run.

#### Delete the assessed eligible branches

| Parameter | Value |
| :--- | :--- |
| `scope` | `all` — match your assessment |
| `include_legacy_candidates` | `false` — match your assessment |
| `retention_days` | `30` — use the same age as your assessment |
| `confirm_delete` | `true` |

Review the new assessment before approving the environment job. A fresh run
reassesses eligibility; the approved plan is revalidated before deletion.

Choose a narrower scope in either table when required:

| `scope` value | Branches assessed |
| :--- | :--- |
| `candidates` | Temporary staging candidates only; workflow default; no age/cycle retention |
| `backups` | Staging backups only; cycle and age retention apply |
| `all` | Both candidates and backups |

The workflow shares the closure concurrency lock and never resets staging or
deletes work/release branches. Review `retention-plan.json` and the reasons in the
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

#### Assess legacy candidates explicitly

| Parameter | Value |
| :--- | :--- |
| `scope` | `candidates` |
| `include_legacy_candidates` | `true` |
| `retention_days` | `30` — ignored for candidates |
| `confirm_delete` | `false` |

This explicitly reviews old names such as `staging-next-cycle` or candidates
whose run can no longer be identified. They still need content-preservation proof, and are kept
if a known preparation is active or waiting. Review the proposed deletions, then
start a new run with the same scope/legacy option and `confirm_delete=true` to
request approved deletion. This scope cannot delete backup branches. Candidates
containing unique work require investigation/preservation or explicit manual
abandonment; this workflow does not discard their unique content.

**Backups only:** the default policy retains the latest **3 verified successful
cycles** and everything younger than **30 days**. The `retention_days` input
defaults to `30`; select `3` for a shorter demo policy without editing code.
The DEV-226 closeout tests used `1`. It must be a positive whole number.
Backups of the latest three successful cycles
are always kept, regardless of age. Both thresholds must be passed before older
backups are eligible for deletion. The chosen
value is stored in the assessment and reused during post-approval revalidation.
Retention is not automatic after every release; it is a separate assessed action.

#### Assess backups with a shorter demo age

| Parameter | Value |
| :--- | :--- |
| `scope` | `backups` |
| `include_legacy_candidates` | `false` |
| `retention_days` | `3` |
| `confirm_delete` | `false` |

Review the listed reasons, then use the same inputs with `confirm_delete=true`
for an approved deletion run. Locally, the equivalent
read-only assessment is `python3 .github/scripts/recovery_retention.py assess --scope backups --min-days 3`
with `GITHUB_REPOSITORY` and GitHub authentication configured. Omitting
`--min-days` uses the script's `DEFAULT_MIN_DAYS` value of 30.

Backup eligibility requires completed full-closure evidence. Schema-1 receipts
and equivalent DEV-219/220 artifacts still require a successful run attempt.
A schema-2 receipt can prove core closure even when the completed attempt failed,
was cancelled or timed out during optional follow-up: retention cross-checks its
completion marker and exact deleted branch SHAs against the approved plan, the
promotion receipt, carryover manifest and staging/deletion log. Active attempts
or missing/mismatched proof remain protected. Historical failed runs without
that independent proof are not retroactively marked complete.
Each ref must still have the recorded SHA, and its content must be accounted
for in current main/staging or verified release evidence. Active and unresolved
uses are retained. An older completed failed/preparation-only hold is resolved
only by a later verified full closure for the same release name, full release
SHA and exact backup SHA. Every shared use must be resolved; newer failures,
active runs, missing timestamps, unknown revisions and unrelated releases still
protect the backup. Resolving a hold does not bypass the three-cycle/age policy,
current content proof, exact SHA checks, approval or leased deletion.
Missing/expired artifacts and legacy names without sufficient proof produce
**Keep**, not deletion permission.
Network/authentication errors block assessment rather than being treated as
missing proof. Artifact retention therefore affects how much historical cleanup
can be verified; unknown legacy backups need separate manual investigation.
These backup safeguards do not permanently protect redundant temporary candidates.

The summary leads with deletion/retention counts, separates temporary candidates
from backups, lists exact deletion SHAs, and groups retained branches by reason.
Scope, legacy-review permission, run states and all relevant refs are recorded
in the plan and revalidated after approval. Changes require a fresh assessment.

For routine operation, assess the intended scope first, review the exact refs
and SHAs, then request approved deletion using the same inputs with
`confirm_delete=true`. No eligible refs is a valid result. Verify main/staging
and retained backup SHAs after release closure or housekeeping.

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
supported work-branch pushes and on pushes/PRs into staging, release branches
and main. Local hooks can be skipped. At the DEV-227 review, the demo's main and
release rulesets require **Validate promotion path** and **PHP and theme checks**;
the standalone **Commit title check** is not yet separately required. Commit
titles remain checked inside the required **Validate promotion path** PR job.
Making the standalone check required is a separate repository-settings action;
retain the existing required checks when applying that configuration elsewhere.

## Protected cleanup credentials

Close release cycle and Clean up recovery branches support an approved automation
user's repository-scoped token, without installing a GitHub App. Separate active
rulesets grant that user only staging force-push and release-deletion exceptions;
normal main/release PR checks and staging deletion protection retain no bypass.

This demo is configured with `CLEANUP_AUTH_MODE=automation-token`, an approved
account's PAT in the `branch-cleanup` environment, and a main-only approval
policy. The account is an existing organization owner, not a dedicated Write-only
automation identity. Its scoped token and the five active rulesets have been
exercised in the demo. The exception belongs to the account, not just its PAT;
repository Admin access alone does not grant a bypass.

See [Keep rulesets active: setup and live validation](docs/keep-rulesets-active.md)
for setup on another repository and the DEV-225 evidence. An unconfigured
repository defaults to the built-in token, which has no operational bypass.
Automation-token mode fails closed on missing credentials or unsafe settings;
the implementation never changes live settings automatically.

## Demo workflow menu

There are seven current workflows. Filenames remain stable for reusable calls
and historical evidence lookup. **Close release cycle** was previously named
**Branch cleanup**; **Clean up recovery branches** was previously **Recovery branch
housekeeping** and covers backups as well as temporary candidates.

| Workflow | Purpose | Trigger |
| --- | --- | --- |
| [Close release cycle](.github/workflows/branch-cleanup.yml) | Assess, preserve carryover, reset staging, remove completed branches; optionally create the next release | Manual or reusable call |
| [Clean up recovery branches](.github/workflows/recovery-housekeeping.yml) | Assess and retire redundant candidates and expired eligible backups | Manual |
| [Create release branch](.github/workflows/create-release-branch.yml) | Create a named release branch from trusted main | Manual |
| [Deploy WordPress wp-content](.github/workflows/deploy.yml) | Demonstrate environment selection; deployment command is currently a placeholder | Push to develop, staging, main or release-* |
| [CI](.github/workflows/ci.yml) | Required PHP/theme checks and release-safety tests; triggers source-release publication | Push or PR |
| [Validate PR source](.github/workflows/validate-pr.yml) | Promotion-path, PR-title and commit-title checks | Relevant pushes and PRs |
| [Publish GitHub source release](.github/workflows/publish-github-release.yml) | Publish a versioned source release after successful main CI | CI completion on main or manual retry |

DEV-226 removed four superseded workflows:
**DEV-215 - Test repository write access**, **Prepare next UAT cycle**,
**Branch cleanup audit**, and **Staging reset readiness check**. Preparation-only
use remains available through **Close release cycle** with `preparation_only=true`;
its assessment and verification replace the standalone audit/readiness reports.
Their shared Python modules remain: current closure, retention and
release-publication code imports them. Candidate retention reads repository-wide
run history so retired preparation workflows can still be identified without
querying a deleted workflow filename. Do not rename **CI** or its required job
names without updating the publisher trigger and repository check settings.

## Demo validation and backlog

No outstanding demo implementation tasks remain from the DEV-225/226 backlog.
DEV-228 addresses the subsequent next-release creation race described below;
its local regression tests pass, with live rollout validation still pending.
The following closeout results were verified on 27 November 2026:

| Check | Result/evidence |
| --- | --- |
| Protected preparation, staging reset, work/release deletion and candidate removal | [DEV-225 validation record](docs/keep-rulesets-active.md#demo-validation-record) |
| Developer Admin account denied forced update/deletion | Equivalent guard copies on a disposable branch; existing rulesets and main/staging unchanged, as recorded above |
| Resolved preparation-only backup hold | [Assessment 45936372212](https://github.com/choice-aus/wordpress-demo-website-1/actions/runs/45936372212) retained the DEV-225 backup under normal three-cycle policy |
| Full DEV-226 closure with renamed workflow | [Run 45936870815](https://github.com/choice-aus/wordpress-demo-website-1/actions/runs/45936870815) reset staging, removed its candidate, and deleted the completed work/release branches |
| Backup retention after the next cycle | [Assessment 45938243157](https://github.com/choice-aus/wordpress-demo-website-1/actions/runs/45938243157) retained the latest three cycles and identified one eligible older backup |
| Approved backup retirement | [Run 45938463167](https://github.com/choice-aus/wordpress-demo-website-1/actions/runs/45938463167) deleted only the assessed older backup `backup/staging-before-reset-f061dd3bb227` |
| Source publication and retry | [`v1.0.4`](https://github.com/choice-aus/wordpress-demo-website-1/releases/tag/v1.0.4) published at the exact DEV-226 merge SHA; its manual retry returned `already-published` |
| Standalone release-branch creation | [Run 45934961617](https://github.com/choice-aus/wordpress-demo-website-1/actions/runs/45934961617) created `release-dev-226` from then-current main |
| Existing release-branch rejection | [Run 45936672212](https://github.com/choice-aus/wordpress-demo-website-1/actions/runs/45936672212) deliberately failed with `already exists; no branch was moved` |
| Chore-branch promotion and cleanup | [Run 45943776763](https://github.com/choice-aus/wordpress-demo-website-1/actions/runs/45943776763) reset staging and deleted `chore/DEV-227`/`release-dev-227`; optional next-release creation then failed because a required check was still running. This exposed the DEV-228 timing/completion-record gap. |

Minor/major label overrides and mismatched-tag rejection remain covered by
automated tests. Their live publication tests are explicitly deferred, not
reported as passed. Unknown legacy backup provenance still requires manual
investigation; no automatic legacy-adoption override is implemented.

All four work-branch prefixes have local automated policy, carryover and cleanup
coverage. `chore/DEV-227` completed live staging/release promotion and branch
cleanup. `fix/DEV-228` exercises the fix prefix next; its live promotion/closure
and the new CI-wait/independent-receipt behavior are pending validation. `hotfix/*`
has not been separately exercised through the live flow. Local commit hooks are
installed in this WSL checkout; each developer checkout needs its own installation.

For DEV-228 rollout, promote through the normal cycle, close the latest release
with an unused `next_release` name, and verify that CI waiting precedes successful
branch creation. Inspect both receipt/result files and assess backup retention.
Local tests cover follow-up failure after completed closure without weakening
proof requirements; exercise a deliberate live failure only with separately
reviewed disposable inputs. No new workflow parameters are required.

## After demo approval (outside this demo repository's scope)

These are team/platform decisions for adopting the strategy, not requirements
for finishing this demo. Keep them as handover notes rather than demo backlog.

- **Choose the VIP deployment model.** Decide whether to fund/configure a hosted
  release slot. Confirm source/build branch mappings and select a production
  deployment-completion signal before making published releases mean deployed
  releases. Establish a reliable guard or operating control against deleting
  any branch still configured on VIP; today that verification is manual.
- **Check production/VIP rollout.** Confirm approval and ownership/rotation for
  the production automation credential; the demo uses an existing organization
  owner's scoped PAT. Verify required checks (including the separate **Commit
  title check**) on the intended VIP repository and install local hooks in each
  remaining developer checkout. Verify that the automation identity cannot
  bypass main/release PR checks or staging deletion protection; those negative
  cases were not exercised by the disposable developer-account probes.
