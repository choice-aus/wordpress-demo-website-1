#!/bin/sh
set -eu
repo_root=$(git rev-parse --show-toplevel)
hooks_path="$repo_root/.githooks"
existing=$(git config --get core.hooksPath || true)
if [ -n "$existing" ] && [ "$existing" != "$hooks_path" ]; then
    echo "Existing core.hooksPath is $existing; integrate these hooks before changing it." >&2
    exit 1
fi
git config --local core.hooksPath "$hooks_path"
echo "Installed commit-msg and pre-push hooks for this checkout."
