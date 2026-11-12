"""Step Functions task: check, run or undo the agent's proposed write.

Input:  {"op": "validate" | "execute" | "undo", "incident_id": "..."}
Output: {"ok": true | false}

This function's IAM role can only write to resources tagged oncall=managed, and a
permission boundary blocks IAM, KMS, deletes and creates everywhere. The checks
here run first, so a refused call is explained to the agent instead of failing
inside AWS, but they are not what keeps the account safe. IAM is.
"""

import json
import os

import boto3
from botocore import xform_name
from botocore.exceptions import ClientError

from shared import store
from shared.policy import proposal_problems, write_problems

MAX_WRITES = int(os.environ.get("MAX_WRITES", "3"))
DENIED_CODES = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "AuthorizationError"}


def run(call):
    client = boto3.client(call["service"])
    res = getattr(client, xform_name(call["operation"]))(**(call.get("params") or {}))
    res.pop("ResponseMetadata", None)
    return json.loads(json.dumps(res, default=str))


def validate(incident):
    problems = proposal_problems(incident.get("proposal"))
    if incident["writes"] >= MAX_WRITES:
        problems.append(f"this incident already used its {MAX_WRITES} writes")
    if problems:
        store.log(incident["id"], "validation_failed", {"problems": problems})
        return {"ok": False}
    store.log(incident["id"], "validated", {})
    return {"ok": True}


def attempt(incident, call, ok_kind, fail_kind):
    try:
        result = run(call)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        kind = "blocked_by_iam" if code in DENIED_CODES else fail_kind
        store.log(incident["id"], kind, {"call": call, "error": str(e)[:1000]})
        return {"ok": False}
    store.log(incident["id"], ok_kind, {"call": call, "result": result})
    return {"ok": True}


def execute(incident):
    proposal = incident["proposal"]
    # Checked again: the proposal must be the one that passed validation and was approved.
    problems = proposal_problems(proposal)
    if problems or incident["writes"] >= MAX_WRITES:
        store.log(incident["id"], "validation_failed", {"problems": problems or ["write limit reached"]})
        return {"ok": False}
    store.count_write(incident["id"])
    store.log(incident["id"], "executing", {"call": proposal["call"]}, status="executing")
    return attempt(incident, proposal["call"], "executed", "execute_failed")


def undo(incident):
    proposal = incident.get("proposal") or {}
    call = proposal.get("undo")
    if not call or write_problems(call):
        store.log(incident["id"], "undo_unavailable", {"reason": "no valid undo call"})
        return {"ok": False}
    store.log(incident["id"], "not_cleared", {"call": proposal["call"],
                                              "note": "the alarm did not clear after this change"})
    return attempt(incident, call, "undone", "undo_failed")


OPS = {"validate": validate, "execute": execute, "undo": undo}


def handler(event, _context):
    return OPS[event["op"]](store.get(event["incident_id"]))
