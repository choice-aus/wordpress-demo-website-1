# Keep rulesets active (DEV-225)

The cleanup workflows support a dedicated automation **user** with a
repository-scoped fine-grained personal access token (PAT). No GitHub App is
installed. GitHub supports a `User` ruleset bypass actor; VIP and organization
approval of the account/token still needs confirmation. This implementation
does not change repository settings or install credentials automatically.

## Protection layout

Bypass applies to an entire ruleset, so operational exceptions must be separate
from normal PR and status-check enforcement. All five rulesets stay **active**:

| Ruleset | Exact scope | Rules | Bypass |
| --- | --- | --- | --- |
| `main-protection` | Default branch (`main`) | Existing deletion, force-push, PR and status rules | None |
| `staging-protection` | `refs/heads/staging` | Deletion; preserve other normal rules | None |
| `release-protection` | `refs/heads/release-*` | Existing force-push, squash PR and status rules | None |
| `staging-reset-guard` | `refs/heads/staging` | Block force pushes only | Automation user's numeric ID, `User`, `always` |
| `release-deletion-guard` | `refs/heads/release-*` | Block deletion only | Same user, `always` |

Do not give the automation user an admin role, or add broad role/team bypasses.
The user can reset staging and delete releases; it cannot use these exceptions
to delete staging, change main directly or bypass normal release PR checks.
GitHub enforces all matching rulesets together. Other organization rulesets or
classic branch protections may still reject operations; investigate those
without disabling protection. Existing SHA verification, approval, carryover
proof, backups, exact leases and atomic cleanup remain in force.

The exception belongs to the **identity**, not a particular workflow. A leaked
PAT would carry the same bypass rights. The environment and token restrictions
below are therefore part of the design.

## Setup after promoting DEV-225 to main

1. Confirm your organization/VIP permits the dedicated account and PAT. Use an
   organization member with access only to the intended repository and **Write**
   repository access, not an outside collaborator or repository administrator.
   Follow your organization's account, 2FA, token approval and expiry policies.
2. Configure the `branch-cleanup` environment **before storing the PAT**:
   retain required reviewers; use **Selected branches and tags**, with exactly
   one rule of type **Branch**, named `main`. Do not allow tags, wildcards or all
   protected branches. Disable administrator bypass of environment protections
   where available. Prevent self-review if your team has another reviewer.
3. Create a fine-grained PAT owned by the automation account, resource owner
   the organization, selected repository only. Permissions:

   | Repository permission | Access | Use |
   | --- | --- | --- |
   | Contents | Read and write | Fetch/push/delete refs; optional next release creation |
   | Workflows | Read and write | Ref updates containing workflow-file changes |
   | Actions | Read | Workflow evidence, artifacts and approval-environment checks |
   | Pull requests | Read | Promotion/merge/PR evidence |
   | Issues | Read | Carryover label evidence |
   | Metadata | Read (automatic) | Repository/ruleset metadata |

   No Administration write permission is required. Do not paste the token into
   chat, Git URLs, command arguments or files in the checkout. Store it only as
   environment secret **`BRANCH_CLEANUP_TOKEN`** in `branch-cleanup`, never as a
   repository/organization secret available to other jobs. Arrange expiry and
   rotation; an expired/unapproved token blocks operations.
4. Generate a **read-only proposal** with your administrator's ordinary local
   `gh` login, substituting the automation account's real username:

   ```sh
   python3 .github/scripts/ruleset_policy.py \
     --repository choice-aus/wordpress-demo-website-1 \
     --actor YOUR_AUTOMATION_LOGIN \
     --output /tmp/cleanup-ruleset-proposal.json
   ```

   The output includes resolved user ID, API paths and complete payloads. It
   never applies them. Re-read current settings and review the proposal before
   an administrator applies it in the listed order. Use GitHub API version
   `2026-03-10` when applying API payloads. Create the active operational guards
   **first**, then remove a duplicate force-push rule from `staging-protection`
   or deletion rule from `release-protection`, if present. Baseline rulesets
   remain active, with their existing PR/check parameters preserved. The current
   demo settings already lack those two restrictions; their proposal only adds
   the two missing active guards.
5. Set repository variables **`CLEANUP_ACTOR`** to the username and
   **`CLEANUP_AUTH_MODE=automation-token`**. Dispatch approved operations from
   **main**. The preflight checks token ownership, main, exact environment branch
   policy, reviewers and all five rulesets before configuring Git credentials.
   Missing secrets, unexpected actors, broad bypasses or disabled/missing guards
   block execution without falling back to the built-in token.

Assessment jobs continue using the read-only built-in token. Only approved
execute jobs use the PAT, for both `gh` and Git. Checkout does not persist a
different write credential; the GitHub CLI credential helper reads `GH_TOKEN`
in the execution step, including separate carryover worktrees. The uploaded
`cleanup-auth-report.json` records mode/identity/rulesets, never the PAT.

Standalone release-branch creation and GitHub Release publishing keep their
built-in token. Creating a fresh release ref from trusted main does not need a
force-push or deletion bypass. PAT-authenticated pushes can trigger ordinary
push workflows; this differs from built-in token pushes. Cleanup remains
manually dispatched and uses the existing shared concurrency lock.

## Validation and rollout

Local unit tests cover ruleset scope/bypass, preserving PR/check rules, ordered
proposal generation, missing/wrong credentials and environment restrictions.
They cannot prove GitHub's live enforcement or organizational token policy.

After review and explicit authorization of the settings changes:

1. Confirm a main assessment succeeds using the built-in read token. Run a
   preparation-only execution after environment approval to exercise PAT
   authentication and evidence collection without resetting staging/deleting
   completed release branches. This still creates candidate and backup refs.
2. With disposable test refs and separately approved writes, verify an ordinary
   writer cannot force-update staging or delete a release; verify the automation
   identity cannot delete staging, directly update main or bypass release PR
   checks. Do not run destructive negative tests against real protected refs.
   A local mock or `git push --dry-run` does not prove server enforcement.
3. Close a completed demo release using the normal workflow. Verify leased
   staging promotion, release/work-branch deletion, candidate removal, retained
   backup, and that all five rulesets stayed active throughout.
4. Run recovery housekeeping first as assessment-only, then approve a planned
   redundant-ref deletion. Verify its authentication report and exact ref SHAs.
   Check standalone release creation and the automatic publisher still work.

Until rollout, unset `CLEANUP_AUTH_MODE` (or `github-token`) retains the existing
built-in-token behavior with **no** protection bypass. Once guards are enabled,
that mode can be rejected by GitHub for protected operations. For a rollback,
revoke/rotate the PAT or disable the privileged workflows; leave guards active.
Do not lift branch restrictions to recover from an authentication error.

## Sources

- [GitHub ruleset API: User bypass actors and complete payloads](https://docs.github.com/en/rest/repos/rules)
- [Rulesets and combined enforcement](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets)
- [Fine-grained PAT scope, organization approval and limitations](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
- [Fine-grained token permissions](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)
- [Environment approval and secret availability](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/review-deployments)
- [GitHub CLI credential helper](https://cli.github.com/manual/gh_auth_setup-git)
