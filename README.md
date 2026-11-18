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

## Development backlog and delivery plan

Work is grouped into three batches so each item does not need its own feature
branch. Ticket branches use `feature/DEV-123` (or an approved work-branch prefix)
without a descriptive suffix and start from current main.

1. **Branch and commit validation (DEV-220, released).** Support `feature/`, `fix/`,
   `hotfix/` and `chore/` consistently in promotion, carryover, preservation and
   cleanup. Share the commit-title rule between installed local hooks and the
   dedicated CI check. Remaining rollout: require **Commit title check** in
   GitHub rulesets and install hooks in each developer checkout.
2. **Release-cycle housekeeping (DEV-221 in main; DEV-222 lifecycle refinement under review).**
   - Extend exact release verification for changes already present in the
     release base, covering the pending `release-dev-215` case without weakening
     binary, mode or content checks. Report excluded branches and their reasons,
     and distinguish an already deleted release from unverified release history.
   - Resolve historical carryover/failure labels using verified later release
     evidence; preserve the audit history and keep current unreleased changes.
   - Separate short-lived candidates from backups. Retire promoted candidates
     by default immediately after verified promotion, independently of later
     release cleanup, and retain promotion evidence in artifacts. Review old,
     safely superseded candidates in candidate-only housekeeping without age or
     cycle-count retention; protect active runs, open PRs and unique changes.
   - Keep backup retention at 3 successful cycles and 30 days normally; demos
     can set `retention_days=3`. Require full-success evidence and current
     content-preservation proof. Preserve failed/active recovery backups.
   - Optionally create the next `release-*` from the verified main SHA using an
     explicit new name. Never move an existing branch or create a next release
     during older-release cleanup. Include it in assessment and revalidation.
3. **Keep rulesets active (investigate before configuration).** Keep staging
   reset and release deletion restrictions enabled, granting the approved
   closure workflow a controlled exception without installing a GitHub App.
   Investigate a repository deploy key or a dedicated automation identity with
   an appropriately scoped token and ruleset bypass. Confirm WordPress VIP and
   organization policies allow the chosen credential. Split operational
   restrictions from promotion/status-check rules so the exception need not
   bypass ordinary PR checks. Protect credentials with the approval environment
   and test rejected writes as well as permitted operations. GitHub supports
   deploy-key and user/team bypass actors, but this has not been configured or
   validated for this repository. See the [GitHub ruleset API](https://docs.github.com/en/rest/repos/rules).

Batch 2 follow-up writes are opt-in and housekeeping remains a separate approved
action. Batch 3 protection exceptions have not been configured. Existing approval,
leased writes and preparation-only restrictions continue to apply.
