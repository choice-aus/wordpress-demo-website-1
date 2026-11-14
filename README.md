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

### Promotion workflow

1. Create feature branches from `main`.
2. Merge feature branches into `staging` as work progresses. Pull requests are optional.
3. Once a feature is approved, create a pull request from `feature/*` into the current `release-*` branch.
4. Use **Squash and merge** when merging features into a release branch.
5. Test the completed release.
6. Create a pull request from `release-*` into `main`.
7. Use **Create a merge commit** when promoting a release into `main`.

### Promotion restrictions

- Only `feature/*` branches may merge into `release-*`.
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
