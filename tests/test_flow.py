"""Control, executor and approval handlers against mocked AWS (moto)."""

import importlib
import json
import os

import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def aws(monkeypatch):
    with mock_aws():
        boto3.client("dynamodb").create_table(
            TableName="oncall-incidents", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}])
        topic = boto3.client("sns").create_topic(Name="oncall-notifications")["TopicArn"]
        monkeypatch.setenv("TOPIC_ARN", topic)
        monkeypatch.setenv("APPROVAL_URL", "https://approve.example")
        from shared import store
        store._table = None
        import control.handler, executor.handler, approval.handler
        mods = [importlib.reload(m) for m in (control.handler, executor.handler, approval.handler)]
        yield dict(zip(["control", "executor", "approval"], mods), store=store)


def alarm_event(name):
    return {"event": {"region": "eu-central-1", "detail": {
        "alarmName": name, "state": {"value": "ALARM", "reason": "Threshold crossed", "timestamp": "now"},
        "configuration": {"description": "test"}}}}


def test_alarms_that_fire_together_share_an_incident(aws):
    c = aws["control"]
    a = c.handler({"op": "open", **alarm_event("demo-api-errors")}, None)
    b = c.handler({"op": "open", **alarm_event("demo-api-5xx")}, None)
    assert not a["duplicate"] and b["duplicate"] and a["incident_id"] == b["incident_id"]
    inc = aws["store"].get(a["incident_id"])
    assert inc["alarms"] == ["demo-api-errors", "demo-api-5xx"]


def propose(store, incident_id, call, undo):
    store.log(incident_id, "diagnosis", {}, diagnosis={"action": "propose", "summary": "s", "root_cause": "r",
                                                       "evidence": []},
              proposal={"call": call, "undo": undo, "expected_effect": "e"})


def test_executor_refuses_and_explains(aws):
    inc = aws["control"].handler({"op": "open", **alarm_event("demo-api-errors")}, None)["incident_id"]
    propose(aws["store"], inc, {"service": "lambda", "operation": "DeleteFunction",
                                "params": {"FunctionName": "demo-orders-api"}}, None)
    assert aws["executor"].handler({"op": "validate", "incident_id": inc}, None) == {"ok": False}
    fb = aws["store"].feedback(aws["store"].get(inc))
    assert fb[0]["kind"] == "validation_failed" and any("deletes" in p for p in fb[0]["problems"])


def test_execute_and_undo(aws):
    lam = boto3.client("lambda")
    role = boto3.client("iam").create_role(RoleName="r", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("handler.py", "def handler(e, c): return 1")
    lam.create_function(FunctionName="demo-orders-worker", Runtime="python3.12", Role=role,
                        Handler="handler.handler", Code={"ZipFile": buf.getvalue()}, Timeout=10)

    inc = aws["control"].handler({"op": "open", **alarm_event("demo-worker-errors")}, None)["incident_id"]
    call = {"service": "lambda", "operation": "UpdateFunctionConfiguration",
            "params": {"FunctionName": "demo-orders-worker", "Timeout": 30}}
    undo = {"service": "lambda", "operation": "UpdateFunctionConfiguration",
            "params": {"FunctionName": "demo-orders-worker", "Timeout": 10}}
    propose(aws["store"], inc, call, undo)
    ex = aws["executor"]
    assert ex.handler({"op": "validate", "incident_id": inc}, None) == {"ok": True}
    assert ex.handler({"op": "execute", "incident_id": inc}, None) == {"ok": True}
    assert lam.get_function_configuration(FunctionName="demo-orders-worker")["Timeout"] == 30
    assert ex.handler({"op": "undo", "incident_id": inc}, None) == {"ok": True}
    assert lam.get_function_configuration(FunctionName="demo-orders-worker")["Timeout"] == 10
    kinds = [e["kind"] for e in aws["store"].get(inc)["events"]]
    assert kinds[-3:] == ["executed", "not_cleared", "undone"]
    assert aws["store"].get(inc)["writes"] == 1


def test_write_limit(aws, monkeypatch):
    inc = aws["control"].handler({"op": "open", **alarm_event("demo-api-errors")}, None)["incident_id"]
    for _ in range(3):
        aws["store"].count_write(inc)
    call = {"service": "lambda", "operation": "UpdateAlias",
            "params": {"FunctionName": "demo-orders-api", "Name": "live", "FunctionVersion": "3"}}
    propose(aws["store"], inc, call, {**call, "params": {**call["params"], "FunctionVersion": "4"}})
    assert aws["executor"].handler({"op": "validate", "incident_id": inc}, None) == {"ok": False}


def http(method, approval_id, body=""):
    return {"pathParameters": {"id": approval_id}, "requestContext": {"http": {"method": method}},
            "body": body, "isBase64Encoded": False}


def test_approval_link_get_is_safe_and_post_works_once(aws, monkeypatch):
    store, ap = aws["store"], aws["approval"]
    inc = aws["control"].handler({"op": "open", **alarm_event("demo-api-errors")}, None)["incident_id"]
    call = {"service": "lambda", "operation": "UpdateAlias",
            "params": {"FunctionName": "demo-orders-api", "Name": "live", "FunctionVersion": "3"}}
    propose(store, inc, call, {**call, "params": {**call["params"], "FunctionVersion": "4"}})
    aws["control"].handler({"op": "request_approval", "incident_id": inc, "task_token": "tok"}, None)
    approval_id = store.get(inc)["approval_id"]

    sent = []
    monkeypatch.setattr(ap._sfn, "send_task_success", lambda **kw: sent.append(kw))
    for _ in range(3):  # scanners opening the link change nothing
        res = ap.handler(http("GET", approval_id), None)
        assert res["statusCode"] == 200 and "UpdateAlias" in res["body"]
    assert sent == []

    res = ap.handler(http("POST", approval_id, "decision=reject&comment=try+the+timeout"), None)
    assert res["statusCode"] == 200
    assert json.loads(sent[0]["output"]) == {"decision": "reject", "comment": "try the timeout"}
    assert ap.handler(http("POST", approval_id, "decision=approve"), None)["statusCode"] == 410
    assert ap.handler(http("GET", "made-up-id"), None)["statusCode"] == 410
    fb = store.feedback(store.get(inc))
    assert fb[-1] == {"kind": "rejected", "comment": "try the timeout"}


def test_html_in_diagnosis_is_escaped(aws):
    store, ap = aws["store"], aws["approval"]
    inc = aws["control"].handler({"op": "open", **alarm_event("demo-api-errors")}, None)["incident_id"]
    call = {"service": "lambda", "operation": "UpdateAlias",
            "params": {"FunctionName": "demo-orders-api", "Name": "live", "FunctionVersion": "3"}}
    store.log(inc, "diagnosis", {}, diagnosis={"summary": "<script>alert(1)</script>", "root_cause": "r",
                                               "evidence": []},
              proposal={"call": call, "undo": call, "expected_effect": "e"})
    aws["control"].handler({"op": "request_approval", "incident_id": inc, "task_token": "tok"}, None)
    body = ap.handler(http("GET", store.get(inc)["approval_id"]), None)["body"]
    assert "<script>" not in body and "&lt;script&gt;" in body
