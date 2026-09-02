#!/usr/bin/env bash
# Aperture end-to-end walkthrough.
#
# Builds a demo workspace and runs the six scenarios that define the product:
# purpose binding, clearance, ACL, tenant isolation, field redaction, and audit.
#
# Usage:  bash examples/walkthrough.sh [workspace_dir]

set -euo pipefail

WORKSPACE="${1:-demo_state/workspace}"
APERTURE="python -m aperture.cli"
export PYTHONPATH="${PYTHONPATH:-src}"

banner() {
    printf '\n\033[1m== %s ==\033[0m\n' "$1"
}

banner "Build the demo workspace"
$APERTURE demo --path "$WORKSPACE"

banner "Validate the governance configuration"
$APERTURE lint -w "$WORKSPACE"

banner "1. Purpose binding: People Ops asking for HR support"
$APERTURE query -w "$WORKSPACE" -p u_dana --purpose hr_support \
    "how much parental leave do we offer"

banner "2. Same question, support purpose: the handbook is not reachable"
$APERTURE query -w "$WORKSPACE" -p u_kim --purpose customer_support \
    "how much parental leave do we offer"

banner "3. Wrong group: support lead declaring hr_support still gets nothing"
$APERTURE query -w "$WORKSPACE" -p u_kim --purpose hr_support "parental leave"

banner "4. Field redaction: the row survives, compensation does not"
$APERTURE query -w "$WORKSPACE" -p u_dana --purpose hr_support \
    "Raj Mehta platform engineer manager location"

banner "5. Tenant isolation: another tenant's chunk is withheld by reason"
$APERTURE query -w "$WORKSPACE" -p u_kim --purpose customer_support \
    "globex partner onboarding seats tenant admin console"

banner "6. What the caller may reach at all"
$APERTURE sources -w "$WORKSPACE" -p u_kim --purpose customer_support

banner "Audit trail"
$APERTURE lineage -w "$WORKSPACE" tail --limit 6

banner "Chain integrity"
$APERTURE lineage -w "$WORKSPACE" verify
