"""The page behind the email link.

GET  /d/{id}  shows the diagnosis and the proposed call, with Approve and Reject buttons.
POST /d/{id}  records the decision and resumes the paused workflow.

GET never changes anything, because email security scanners open links on
their own. The link ID is a random 192-bit value, works once and expires
with the task token.
"""

import base64
import html
import json
from urllib.parse import parse_qs

import boto3

from shared import store

_sfn = boto3.client("stepfunctions")

PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>oncall</title><style>
body{{font:15px/1.5 system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;color:#111}}
pre{{background:#f4f4f4;padding:.8rem;overflow:auto;border-radius:6px}}
textarea{{width:100%;height:5rem}} button{{font-size:15px;padding:.5rem 1.2rem;margin-right:.6rem}}
.warn{{color:#a00;font-weight:600}}</style></head><body>{body}</body></html>"""


def page(body, status=200):
    return {"statusCode": status, "headers": {"content-type": "text/html; charset=utf-8",
                                              "cache-control": "no-store", "referrer-policy": "no-referrer"},
            "body": PAGE.format(body=body)}


def esc(v):
    return html.escape(v if isinstance(v, str) else json.dumps(v, indent=2, default=str))


def show(approval_id, incident):
    diag, proposal = incident["diagnosis"], incident["proposal"]
    evidence = "".join(f"<li>{esc(e['claim'])} <small>[{esc(', '.join(e.get('refs', [])))}]</small></li>"
                       for e in diag.get("evidence", []))
    undo = ('<p class="warn">Irreversible: there is no undo.</p>' if proposal.get("irreversible")
            else f"<h3>Undo</h3><pre>{esc(proposal.get('undo'))}</pre>")
    return page(f"""
<h2>Incident {esc(incident['id'])}</h2>
<p><b>Alarms:</b> {esc(', '.join(incident['alarms']))}</p>
<p>{esc(diag.get('summary', ''))}</p>
<p><b>Root cause:</b> {esc(diag.get('root_cause', ''))}</p>
<ul>{evidence}</ul>
<h3>Proposed change</h3><pre>{esc(proposal['call'])}</pre>{undo}
<p><b>Expected effect:</b> {esc(proposal.get('expected_effect', ''))}</p>
<form method="post">
<p><textarea name="comment" placeholder="Optional. On reject, this goes back to the agent."></textarea></p>
<button name="decision" value="approve">Approve</button>
<button name="decision" value="reject">Reject</button>
</form>""")


def decide(approval_id, body):
    form = parse_qs(body)
    decision = (form.get("decision") or [""])[0]
    comment = (form.get("comment") or [""])[0][:2000]
    if decision not in ("approve", "reject"):
        return page("<p>Unknown decision.</p>", 400)
    approval = store.use_approval(approval_id)
    if not approval:
        return page("<p>This link was already used or has expired.</p>", 410)
    incident_id = approval["incident_id"]
    if decision == "reject":
        store.log(incident_id, "rejected", {"comment": comment or "(no comment)"})
    else:
        store.log(incident_id, "approved", {"comment": comment})
    try:
        _sfn.send_task_success(taskToken=approval["task_token"],
                               output=json.dumps({"decision": decision, "comment": comment}))
    except (_sfn.exceptions.TaskTimedOut, _sfn.exceptions.InvalidToken, _sfn.exceptions.TaskDoesNotExist):
        return page("<p>The incident is no longer waiting for a decision.</p>", 410)
    done = "Approved. The change runs now, and you get an email with the result." if decision == "approve" \
        else "Rejected. Your comment went back to the agent."
    return page(f"<p>{done}</p>")


def handler(event, _context):
    approval_id = (event.get("pathParameters") or {}).get("id", "")
    method = event["requestContext"]["http"]["method"]
    if method == "POST":
        body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode()
        return decide(approval_id, body)
    approval = store.get_approval(approval_id)
    if not approval:
        return page("<p>This link was already used or has expired.</p>", 410)
    return show(approval_id, store.get(approval["incident_id"]))
