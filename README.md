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
  environment approval. Each feature's exact combined PR diff must match its
  squash diff in main; the release must have reached main through a merge commit.
- `audit_mode=specific` selecting an older completed release allows verified branch
  cleanup only. It never prepares a candidate or resets staging. Preparation-only
  mode on an older release performs no writes.

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

Backups use `backup/staging-before-reset-<original-SHA-prefix>` and are never
moved or deleted. Candidates use `staging-next-cycle-<run-id>-<attempt>` so a rerun
preserves previous preparation results. The existing candidate
`staging-next-cycle-43378704931` and original backup are retained.
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
changes after staging promotion, cleanup stops and the verified candidate and
backup remain available; the workflow does not attempt a rollback.

Run the release safety tests locally with:

```sh
python3 -m unittest discover -s tests -v
```

The tests use temporary Git repositories and mock all pushes; they do not modify
remote branches. Preparation-only Actions runs do write candidate and backup
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
branch. The current local feature starts with batch 1; subsequent batches can
be reviewed independently and use new branches from main after promotion.

1. **Branch and commit validation (current batch).** Support `feature/`, `fix/`,
   `hotfix/` and `chore/` consistently in promotion, carryover, preservation and
   cleanup. Share the commit-title rule between installed local hooks and the
   dedicated CI check. Remaining rollout: require **Commit title check** in
   GitHub rulesets and install hooks in each developer checkout.
2. **Release-cycle housekeeping (next batch).**
   - Extend exact release verification for changes already present in the
     release base, covering the pending `release-dev-215` case without weakening
     binary, mode or content checks. Report excluded branches and their reasons,
     and distinguish an already deleted release from unverified release history.
   - Resolve historical carryover/failure labels using verified later release
     evidence; preserve the audit history and keep current unreleased changes.
   - Handle `staging-next-cycle-*` candidates and `backup/staging-before-reset-*`
     together with an explicit retention plan, assessment and SHA leases.
     Preserve active and failed preparations and their recovery refs. Agree
     retention count/age and evidence requirements before deleting older refs;
     branch age alone is insufficient proof that a backup is disposable.
   - After a latest-release run successfully promotes staging and deletes its
     eligible work/release branches, optionally remove that run's temporary
     candidate using its exact SHA. Keep preparation-only and failed-run
     candidates. Backups follow the separate agreed retention policy.
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

No automated candidate/backup retention, next-release creation, or protection
exception is enabled by batch 1. Existing cleanup approval, backup preservation,
leased writes and preparation-only behavior continue to apply.
