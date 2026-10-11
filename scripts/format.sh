#!/usr/bin/env bash

set -euo pipefail

CDPATH= cd -P -- "$(dirname "${BASH_SOURCE[0]}")/.."

[[ -x .venv/bin/ruff ]] || {
    printf 'Missing .venv tooling; run scripts/rebuild-env.sh\n' >&2
    exit 1
}
[[ -x node_modules/.bin/markdownlint-cli2 ]] || {
    printf 'Missing Node tooling; run scripts/rebuild-env.sh\n' >&2
    exit 1
}

.venv/bin/ruff check --extend-exclude scripts/_shared --fix .
.venv/bin/ruff format --extend-exclude scripts/_shared .
node_modules/.bin/markdownlint-cli2 --fix
