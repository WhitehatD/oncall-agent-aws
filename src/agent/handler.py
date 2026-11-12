"""Step Functions task: investigate one incident and record the outcome.

Input:  {"incident_id": "...", "attempt": 1}
Output: {"action": "propose" | "advise" | "escalate"}
"""

import os

import boto3

from agent import tools
from agent.graph import Budget, investigate
from shared import store

_llm = None


def investigator_session(incident_id):
    """The tools run as a separate read-only role. This function's own role can
    read the API key; the role the model's tools use cannot."""
    creds = boto3.client("sts").assume_role(
        RoleArn=os.environ["INVESTIGATOR_ROLE_ARN"],
        RoleSessionName=f"incident-{incident_id}"[:64],
        DurationSeconds=3600,
    )["Credentials"]
    return boto3.Session(aws_access_key_id=creds["AccessKeyId"],
                         aws_secret_access_key=creds["SecretAccessKey"],
                         aws_session_token=creds["SessionToken"])


def llm():
    global _llm
    if _llm is None:
        from langchain_openai import ChatOpenAI

        key = boto3.client("ssm").get_parameter(Name=os.environ["API_KEY_PARAM"], WithDecryption=True)
        _llm = ChatOpenAI(
            model=os.environ["MODEL"],
            base_url=os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
            api_key=key["Parameter"]["Value"],
            temperature=0,
            max_tokens=4000,
            timeout=90,
            max_retries=2,
            extra_body={"usage": {"include": True}},
            default_headers={"X-Title": "oncall-agent-aws"},
        )
    return _llm


def handler(event, _context):
    incident_id, attempt = event["incident_id"], int(event.get("attempt", 1))
    incident = store.get(incident_id)
    store.log(incident_id, "investigating", {"attempt": attempt, "model": os.environ["MODEL"]},
              status="investigating")

    budget = Budget(max_tool_calls=int(os.environ.get("MAX_TOOL_CALLS", "25")),
                    max_cost_usd=float(os.environ.get("MAX_COST_USD", "0.05")))
    try:
        tools.use_session(investigator_session(incident_id))
        out = investigate(llm(), incident, store.feedback(incident), attempt, budget,
                          system_prefix=os.environ.get("SYSTEM_PREFIX", "oncall-"),
                          max_attempts=int(os.environ.get("MAX_ATTEMPTS", "3")))
        result = out["final"]
        store.add_usage(incident_id, out["cost_usd"], out["tokens"])
        trace, tool_calls = out["trace"], out["tool_calls"]
    except Exception as e:  # noqa: BLE001 - a crash becomes an escalation, not a silent failure
        result = {"action": "escalate", "summary": f"The investigation failed: {type(e).__name__}: {e}"[:800],
                  "root_cause": "unknown", "evidence": []}
        trace, tool_calls = [], 0

    store.log(incident_id, "diagnosis",
              {"attempt": attempt, "action": result["action"], "tool_calls": tool_calls, "trace": trace},
              diagnosis=result, proposal=result.get("proposal") or {})
    return {"action": result["action"]}
