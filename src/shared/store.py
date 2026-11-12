"""The incidents table. One table, three kinds of item:

  INC#<id>   an incident: alarms, status, diagnosis, proposal, event log, cost
  APR#<id>   a pending approval: the Step Functions task token behind an email link
  CURRENT    points at the open incident, so alarms that fire together share one
"""

import json
import os
import secrets
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr

GROUP_WINDOW_S = int(os.environ.get("GROUP_WINDOW_S", "900"))
TERMINAL = {"resolved", "advised", "escalated", "timeout", "failed"}

_table = None


def table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["INCIDENTS_TABLE"])
    return _table


def now():
    return int(time.time())


def _event(kind, data):
    return {"t": now(), "kind": kind, "data": json.dumps(data, default=str)}


def open_or_attach(alarm):
    """Open a new incident for this alarm, or attach it to the one already open.

    Returns (incident_id, attached)."""
    t = table()
    current = t.get_item(Key={"pk": "CURRENT"}).get("Item")
    if current and now() - int(current["opened_at"]) < GROUP_WINDOW_S:
        inc = get(current["incident_id"])
        if inc and inc["status"] not in TERMINAL:
            if alarm["name"] not in inc["alarms"]:
                t.update_item(
                    Key={"pk": "INC#" + inc["id"]},
                    UpdateExpression="SET alarms = list_append(alarms, :a), events = list_append(events, :e)",
                    ExpressionAttributeValues={":a": [alarm["name"]], ":e": [_event("alarm_attached", alarm)]},
                )
            return inc["id"], True

    incident_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    t.put_item(Item={
        "pk": "INC#" + incident_id,
        "id": incident_id,
        "status": "open",
        "created_at": now(),
        "alarms": [alarm["name"]],
        "first_alarm": json.dumps(alarm, default=str),
        "events": [_event("opened", alarm)],
        "writes": 0,
        "cost_usd": Decimal("0"),
        "tokens": 0,
    })
    t.put_item(Item={"pk": "CURRENT", "incident_id": incident_id, "opened_at": now()})
    return incident_id, False


def get(incident_id):
    item = table().get_item(Key={"pk": "INC#" + incident_id}, ConsistentRead=True).get("Item")
    return _decode(item) if item else None


def _decode(item):
    out = dict(item)
    for key in ("first_alarm", "diagnosis", "proposal"):
        if isinstance(out.get(key), str):
            out[key] = json.loads(out[key])
    out["events"] = [{**e, "data": json.loads(e["data"])} for e in out.get("events", [])]
    out["writes"] = int(out.get("writes", 0))
    out["tokens"] = int(out.get("tokens", 0))
    out["cost_usd"] = float(out.get("cost_usd", 0))
    return out


def log(incident_id, kind, data, status=None, **attrs):
    """Append an event, optionally setting the status and other attributes."""
    expr = ["events = list_append(events, :e)"]
    values = {":e": [_event(kind, data)]}
    names = {}
    if status:
        expr.append("#s = :s")
        values[":s"] = status
        names["#s"] = "status"
    for i, (k, v) in enumerate(attrs.items()):
        expr.append(f"#a{i} = :a{i}")
        names[f"#a{i}"] = k
        values[f":a{i}"] = json.dumps(v, default=str) if isinstance(v, (dict, list)) else v
    kwargs = {"ExpressionAttributeNames": names} if names else {}
    table().update_item(
        Key={"pk": "INC#" + incident_id},
        UpdateExpression="SET " + ", ".join(expr),
        ExpressionAttributeValues=values,
        **kwargs,
    )


def add_usage(incident_id, cost_usd, tokens):
    table().update_item(
        Key={"pk": "INC#" + incident_id},
        UpdateExpression="ADD cost_usd :c, tokens :k",
        ExpressionAttributeValues={":c": Decimal(str(round(cost_usd, 6))), ":k": int(tokens)},
    )


def count_write(incident_id):
    table().update_item(Key={"pk": "INC#" + incident_id}, UpdateExpression="ADD writes :one",
                        ExpressionAttributeValues={":one": 1})


def close(incident_id, status, data):
    log(incident_id, status, data, status=status, closed_at=now())
    try:
        table().delete_item(Key={"pk": "CURRENT"}, ConditionExpression=Attr("incident_id").eq(incident_id))
    except table().meta.client.exceptions.ConditionalCheckFailedException:
        pass


def feedback(incident):
    """What the agent should know from earlier attempts on this incident."""
    kinds = {"validation_failed", "rejected", "execute_failed", "blocked_by_iam", "not_cleared", "undone"}
    return [{"kind": e["kind"], **e["data"]} for e in incident["events"] if e["kind"] in kinds]


def put_approval(incident_id, task_token, ttl_s=3600):
    approval_id = secrets.token_urlsafe(24)
    table().put_item(Item={
        "pk": "APR#" + approval_id,
        "incident_id": incident_id,
        "task_token": task_token,
        "used": False,
        "expires_at": now() + ttl_s,
    })
    return approval_id


def get_approval(approval_id):
    item = table().get_item(Key={"pk": "APR#" + approval_id}, ConsistentRead=True).get("Item")
    if not item or item["used"] or int(item["expires_at"]) < now():
        return None
    return item


def use_approval(approval_id):
    """Mark the link as used. Returns the item, or None if it was already used or expired."""
    try:
        res = table().update_item(
            Key={"pk": "APR#" + approval_id},
            UpdateExpression="SET used = :t",
            ConditionExpression=Attr("used").eq(False) & Attr("expires_at").gte(now()),
            ExpressionAttributeValues={":t": True},
            ReturnValues="ALL_NEW",
        )
        return res["Attributes"]
    except table().meta.client.exceptions.ConditionalCheckFailedException:
        return None
