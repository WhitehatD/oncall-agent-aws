#!/usr/bin/env bash
# Deploy everything: build the Lambda packages, then terraform apply.
#   scripts/up.sh          (about 6 minutes; needs infra/terraform.tfvars and AWS credentials)
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f infra/terraform.tfvars ] || { echo "copy infra/terraform.tfvars.example to infra/terraform.tfvars first" >&2; exit 1; }
PY=$( [ -x .venv/Scripts/python ] && echo .venv/Scripts/python || echo .venv/bin/python )
[ -x "$PY" ] || { python -m venv .venv && PY=$( [ -x .venv/Scripts/python ] && echo .venv/Scripts/python || echo .venv/bin/python ); "$PY" -m pip install -q -r requirements-dev.txt; }
"$PY" scripts/build.py
terraform -chdir=infra init -input=false >/dev/null
terraform -chdir=infra apply -input=false -auto-approve
echo "up. Break something: $PY scripts/chaos.py break bad_deploy"
