#!/usr/bin/env bash

set -euo pipefail

CDPATH= cd -P -- "$(dirname "${BASH_SOURCE[0]}")/.."
repository_root=$PWD

scripts/check-fast.sh
.venv/bin/python scripts/_shared/h2h_dev_tools/git_environment.py \
    --repository "$repository_root" -- \
    .venv/bin/python scripts/pytest-source.py --check-backend-pairs

.venv/bin/python scripts/_shared/h2h_dev_tools/offline_wheel.py \
    --repository "$repository_root" \
    --smoke-script scripts/smoke-installed-dependencies.py
