from unittest.mock import patch

from agent import graph as g
from agent import tools
from fake_llm import ScriptedLLM, read, submit

INCIDENT = {"id": "t1", "alarms": ["demo-api-errors"], "first_alarm": {"name": "demo-api-errors"}}

ROLLBACK = {
    "action": "propose",
    "summary": "v4 throws on every order; v3 did not.",
    "root_cause": "bad deploy of demo-orders-api v4",
    "evidence": [{"claim": "live points at v4", "refs": ["R1"]}],
    "ruled_out": [{"hypothesis": "throttling", "reason": "no throttles", "refs": []}],
    "proposal": {
        "call": {"service": "lambda", "operation": "UpdateAlias",
                 "params": {"FunctionName": "demo-orders-api", "Name": "live", "FunctionVersion": "3"}},
        "undo": {"service": "lambda", "operation": "UpdateAlias",
                 "params": {"FunctionName": "demo-orders-api", "Name": "live", "FunctionVersion": "4"}},
        "expected_effect": "errors stop within 2 minutes",
    },
}


def fake_client(_service):
    class C:
        def get_alias(self, **kw):
            return {"FunctionVersion": "4", "ResponseMetadata": {}}
    return C()


def run(script, budget=None):
    llm = ScriptedLLM(script=script, calls=[], bound=[])
    with patch.object(tools, "client", fake_client):
        out = g.investigate(llm, INCIDENT, [], 1, budget or g.Budget())
    return out, llm


def test_accepts_cited_proposal():
    out, _ = run([[read("lambda", "GetAlias", FunctionName="demo-orders-api", Name="live")], [submit(**ROLLBACK)]])
    assert out["final"]["action"] == "propose"
    assert out["tool_calls"] == 1
    assert out["trace"][0]["id"] == "R1" and out["trace"][0]["ok"]
    assert out["cost_usd"] > 0


def test_rejects_evidence_that_cites_nothing_real_then_accepts_fix():
    bad = {**ROLLBACK, "evidence": [{"claim": "live points at v4", "refs": ["R9"]}]}
    out, llm = run([[read("lambda", "GetAlias", FunctionName="demo-orders-api", Name="live")],
                    [submit(**bad)], [submit(**ROLLBACK)]])
    assert out["final"]["action"] == "propose"
    rejected = [t for t in out["trace"] if t.get("check") == "rejected"]
    assert rejected and "R9" in rejected[0]["problems"][0]


def test_rejects_a_write_the_executor_would_refuse():
    bad = {**ROLLBACK, "proposal": {**ROLLBACK["proposal"], "call": {
        "service": "iam", "operation": "PutRolePolicy",
        "params": {"RoleName": "r", "PolicyName": "p", "PolicyDocument": "{}"}}}}
    out, _ = run([[read("lambda", "GetAlias", FunctionName="demo-orders-api", Name="live")],
                  [submit(**bad)], [submit(**bad)], [submit(**bad)]])
    assert out["final"]["action"] == "escalate"
    assert any("iam" in p for p in out["final"]["check_problems"])


def test_refused_read_is_shown_to_the_model_not_raised():
    out, llm = run([[read("dynamodb", "Scan", TableName="demo-orders")],
                    [submit(action="escalate", summary="nothing", root_cause="unknown", evidence=[])]])
    assert out["trace"][0]["ok"] is False and "refused" in out["trace"][0]["result"]
    assert out["final"]["action"] == "escalate"


def test_budget_forces_submit():
    script = [[read("lambda", "GetAlias", FunctionName="demo-orders-api", Name="live")]] * 3
    script += [[submit(action="escalate", summary="out of budget", root_cause="unknown", evidence=[])]]
    out, llm = run(script, g.Budget(max_tool_calls=3))
    assert out["tool_calls"] == 3
    assert llm.bound[-1] == (["submit"], "submit")
    assert out["final"]["action"] == "escalate"


def test_plain_text_reply_gets_nudged():
    out, _ = run(["I think it is a deploy.", [read("lambda", "GetAlias", FunctionName="demo-orders-api", Name="live")],
                  [submit(**ROLLBACK)]])
    assert out["final"]["action"] == "propose"


def test_alarm_is_citable_and_failed_results_are_not_evidence():
    ok = {**ROLLBACK, "evidence": [{"claim": "errors alarm fired", "refs": ["A1"]}]}
    out, _ = run([[submit(**ok)]])
    assert out["final"]["action"] == "propose"
    bad = {**ROLLBACK, "evidence": [{"claim": "table is empty", "refs": ["R1"]}]}
    out, _ = run([[read("dynamodb", "Scan", TableName="demo-orders")], [submit(**bad)], [submit(**ok)]])
    rejected = [t for t in out["trace"] if t.get("check") == "rejected"]
    assert rejected and "refused or failed" in rejected[0]["problems"][0]


def test_env_decrypt_error_is_relabelled():
    res = tools.hide_env_error({"FunctionName": "x", "Environment": {"Error": {"ErrorCode": "AccessDeniedException"}}})
    assert "hidden" in res["Environment"]["note"]


def test_proposal_sent_as_json_string_is_accepted():
    import json as _json
    as_string = {**ROLLBACK, "proposal": _json.dumps(ROLLBACK["proposal"])}
    out, _ = run([[read("lambda", "GetAlias", FunctionName="demo-orders-api", Name="live")], [submit(**as_string)]])
    assert out["final"]["action"] == "propose"
    assert out["final"]["proposal"]["call"]["operation"] == "UpdateAlias"


def test_leaked_parameter_markup_is_recovered():
    from agent.graph import decode_strings
    leaked = {
        "action": "propose", "summary": "timeout cut to 1s",
        "root_cause": 'timeout lowered to 1s</root_cause>\n<parameter name="evidence">'
                      '[{"claim": "timeout is 1", "refs": ["R4"]}]</parameter>\n'
                      '<parameter name="proposal">{"call": {"service": "lambda"}}</parameter>',
    }
    out = decode_strings(leaked)
    assert out["root_cause"] == "timeout lowered to 1s"
    assert out["evidence"] == [{"claim": "timeout is 1", "refs": ["R4"]}]
    assert out["proposal"] == {"call": {"service": "lambda"}}


def test_extra_brace_and_xml_tag_leaks_are_recovered():
    from agent.graph import decode_strings
    out = decode_strings({
        "action": "propose", "summary": "s",
        "proposal": '{"call": {"service": "lambda", "operation": "UpdateAlias"}}}',
        "root_cause": 'bad deploy</root_cause>\n<evidence>[{"claim": "c", "refs": ["R1"]}]</evidence>\n'
                      '<ruled_out>[]</ruled_out>',
    })
    assert out["proposal"] == {"call": {"service": "lambda", "operation": "UpdateAlias"}}
    assert out["root_cause"] == "bad deploy"
    assert out["evidence"] == [{"claim": "c", "refs": ["R1"]}]
    assert out["ruled_out"] == []
