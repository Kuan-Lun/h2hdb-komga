#!/usr/bin/env bash

set -euo pipefail

repository_root="$(git rev-parse --show-toplevel)"
cd "$repository_root"

scripts/check-fast.sh
.venv/bin/pytest --check-backend-pairs

.venv/bin/python scripts/_shared/h2h_dev_tools/offline_wheel.py \
    --repository "$repository_root" \
    --smoke-script scripts/smoke-installed-dependencies.py
