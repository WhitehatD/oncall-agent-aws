#!/usr/bin/env bash
# Delete everything this project created in AWS.
set -euo pipefail
cd "$(dirname "$0")/.."
terraform -chdir=infra destroy -input=false -auto-approve
