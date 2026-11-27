"""Wait for push CI without changing or bypassing required-check rules."""

import time

from branch_audit import github


def push_ci_runs(repository, sha):
    runs = []
    page = 1
    while True:
        batch = github('api', f'repos/{repository}/actions/workflows/ci.yml/runs'
                       f'?head_sha={sha}&event=push&per_page=100&page={page}')['workflow_runs']
        runs.extend(run for run in batch if run.get('head_sha') == sha and run.get('event') == 'push')
        if len(batch) < 100:
            return runs
        page += 1


def wait_for_ci(repository, sha, revalidate, timeout=600, interval=10, quiet_period=15):
    """Wait for active same-SHA CI, then a short stable successful interval.

    PAT pushes to candidates/staging can start fresh CI for a SHA that already
    passed on main. The quiet interval allows delayed events to become visible.
    GitHub still makes the authoritative decision when the ref is created.
    Only Actions read permission is used; other required-check providers remain
    enforced by GitHub. Authentication/network errors propagate, never mean ready.
    """
    if timeout <= 0 or interval <= 0 or quiet_period < 0:
        raise RuntimeError('Invalid CI wait bounds')
    deadline = time.monotonic() + timeout
    stable_since = None
    previous = None
    while True:
        if time.monotonic() >= deadline:
            raise RuntimeError(f'Timed out waiting for CI on {sha}; no release branch created')
        revalidate()
        runs = push_ci_runs(repository, sha)
        signature = sorted((run['id'], run.get('run_attempt', 1), run['status'], run.get('conclusion'))
                           for run in runs)
        active = any(run['status'] != 'completed' for run in runs)
        if runs and not active:
            latest = max(runs, key=lambda run: int(run['id']))
            if latest.get('conclusion') != 'success':
                raise RuntimeError(f"Latest push CI run {latest['id']} ended "
                                   f"{latest.get('conclusion')}; no release branch created")
            if stable_since is None or signature != previous:
                stable_since = time.monotonic()
            if time.monotonic() - stable_since >= quiet_period:
                print(f'Push CI settled successfully for {sha}; required rules remain enabled')
                return
        else:
            stable_since = None
        previous = signature
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f'Timed out waiting for CI on {sha}; no release branch created')
        print(f'Waiting for push CI on {sha}; rechecking approved refs before each poll', flush=True)
        time.sleep(min(interval, remaining))
