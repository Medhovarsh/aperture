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

banner "7. ACTIONS: what may this agent actually do?"
$APERTURE actions -w "$WORKSPACE" list -p svc_support_agent --purpose customer_support

banner "8. Small refund: under the free limit, no human needed"
$APERTURE actions -w "$WORKSPACE" propose support.refund \
    -p svc_support_agent --purpose customer_support \
    --arg customer_id=cus-5510 --arg amount=50

banner "9. Large refund: blast radius priced, human required"
$APERTURE actions -w "$WORKSPACE" propose support.refund \
    -p svc_support_agent --purpose customer_support \
    --arg customer_id=cus-4471 --arg amount=3000

PROPOSAL=$($APERTURE actions -w "$WORKSPACE" pending | grep -o 'prp_[a-f0-9]*' | head -1)

banner "10. The proposer tries to approve itself"
$APERTURE actions -w "$WORKSPACE" approve "$PROPOSAL" --as svc_support_agent || true

banner "11. A support lead approves, then the agent executes"
$APERTURE actions -w "$WORKSPACE" approve "$PROPOSAL" --as u_kim --note "verified duplicate charge"
$APERTURE actions -w "$WORKSPACE" execute "$PROPOSAL" -p svc_support_agent

banner "12. Undo it"
EXECUTION=$($APERTURE actions -w "$WORKSPACE" history | grep -o 'exe_[a-f0-9]*' | head -1)
$APERTURE actions -w "$WORKSPACE" rollback "$EXECUTION" -p u_kim

banner "13. A refund beyond every grant"
$APERTURE actions -w "$WORKSPACE" propose support.refund \
    -p svc_support_agent --purpose customer_support \
    --arg customer_id=cus-4471 --arg amount=9000 || true

banner "14. One short argument, seven deleted accounts"
$APERTURE actions -w "$WORKSPACE" propose ops.purge_region \
    -p u_ops --purpose data_retention --arg region=legacy || true

banner "15. Read access is not action authority"
$APERTURE actions -w "$WORKSPACE" propose support.refund \
    -p u_dana --purpose customer_support \
    --arg customer_id=cus-4471 --arg amount=10 || true

banner "Audit trail now covers reads and actions in one chain"
$APERTURE lineage -w "$WORKSPACE" verify
