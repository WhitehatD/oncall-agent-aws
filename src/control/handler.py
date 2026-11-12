"""Step Functions tasks that are not the agent or the executor.

  open              EventBridge alarm event -> {"incident_id", "duplicate"}
  request_approval  save the task token, email the approval link (the workflow pauses)
  verify            {"cleared": true} once every alarm in the incident has left ALARM
  notify            final email, then close the incident with its outcome
"""

import json
import os

import boto3

from shared import store

_sns = boto3.client("sns")
_cw = boto3.client("cloudwatch")

APPROVAL_TTL_S = int(os.environ.get("APPROVAL_TTL_S", "3600"))


def op_open(event):
    ev = event["event"]
    d = ev["detail"]
    alarm = {
        "name": d["alarmName"],
        "state": d["state"]["value"],
        "reason": d["state"].get("reason"),
        "since": d["state"].get("timestamp"),
        "description": d.get("configuration", {}).get("description"),
        "metrics": d.get("configuration", {}).get("metrics"),
        "region": ev.get("region"),
    }
    incident_id, attached = store.open_or_attach(alarm)
    return {"incident_id": incident_id, "duplicate": attached}


def _call_text(call):
    if not call:
        return "(none)"
    return f"{call['service']} {call['operation']}\n{json.dumps(call.get('params') or {}, indent=2)}"


def _diagnosis_text(diag):
    lines = [diag.get("summary", ""), "", "Root cause: " + diag.get("root_cause", "")]
    if diag.get("evidence"):
        lines += ["", "Evidence:"] + [f"  - {e['claim']} [{', '.join(e.get('refs', []))}]" for e in diag["evidence"]]
    if diag.get("ruled_out"):
        lines += ["", "Ruled out:"] + [f"  - {r['hypothesis']}: {r['reason']}" for r in diag["ruled_out"]]
    return "\n".join(lines)


def _publish(subject, body):
    _sns.publish(TopicArn=os.environ["TOPIC_ARN"], Subject=subject[:99], Message=body)


def op_request_approval(event):
    incident = store.get(event["incident_id"])
    proposal = incident["proposal"]
    approval_id = store.put_approval(incident["id"], event["task_token"], APPROVAL_TTL_S)
    store.log(incident["id"], "awaiting_approval", {"approval_id_prefix": approval_id[:6]},
              status="awaiting_approval", approval_id=approval_id)
    link = f"{os.environ['APPROVAL_URL'].rstrip('/')}/d/{approval_id}"
    body = "\n".join([
        f"Incident {incident['id']} ({', '.join(incident['alarms'])})",
        "",
        _diagnosis_text(incident["diagnosis"]),
        "",
        "Proposed change:",
        _call_text(proposal["call"]),
        "",
        "Undo:" if not proposal.get("irreversible") else "IRREVERSIBLE: this change has no undo.",
        _call_text(proposal.get("undo")) if not proposal.get("irreversible") else "",
        "",
        "Expected effect: " + proposal.get("expected_effect", ""),
        "",
        f"Approve or reject (link valid for {APPROVAL_TTL_S // 60} minutes):",
        link,
    ])
    _publish(f"[oncall] Approve fix for {incident['alarms'][0]}?", body)
    return {}


def op_verify(event):
    incident = store.get(event["incident_id"])
    alarms = _cw.describe_alarms(AlarmNames=incident["alarms"])["MetricAlarms"]
    states = {a["AlarmName"]: a["StateValue"] for a in alarms}
    cleared = all(s != "ALARM" for s in states.values())
    store.log(incident["id"], "verify", {"states": states, "cleared": cleared}, status="verifying")
    return {"cleared": cleared}


SUBJECTS = {
    "resolved": "Resolved",
    "advised": "Needs you (outside the agent's bounds)",
    "escalated": "Escalated to you",
    "timeout": "No decision, closed",
    "failed": "The workflow failed",
}


def op_notify(event):
    incident = store.get(event["incident_id"])
    outcome = event["outcome"]
    diag = incident.get("diagnosis") or {}
    lines = [f"Incident {incident['id']}: {SUBJECTS[outcome]}", f"Alarms: {', '.join(incident['alarms'])}", ""]
    if diag:
        lines.append(_diagnosis_text(diag))
    if outcome == "advised" and diag.get("advice"):
        lines += ["", "Change to make by hand:", diag["advice"]]
    if outcome == "resolved":
        lines += ["", "Applied:", _call_text(incident["proposal"]["call"])]
    if event.get("error"):
        lines += ["", "Error: " + json.dumps(event["error"], default=str)[:1500]]
    lines += ["", f"Model cost: ${incident['cost_usd']:.4f}, {incident['tokens']} tokens, {incident['writes']} write(s)."]
    _publish(f"[oncall] {SUBJECTS[outcome]}: {incident['alarms'][0]}", "\n".join(lines))
    store.close(incident["id"], outcome, {"error": event.get("error")} if event.get("error") else {})
    return {}


OPS = {"open": op_open, "request_approval": op_request_approval, "verify": op_verify, "notify": op_notify}


def handler(event, _context):
    return OPS[event["op"]](event)
