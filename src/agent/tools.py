"""The agent's read tools. Each one runs with the agent's own IAM role, which is
read-only, and passes through policy.check_read first."""

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import boto3
from botocore import xform_name
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from shared.policy import check_read

MAX_RESULT_CHARS = 6000
_session = boto3.Session()
_clients = {}


def use_session(session):
    """Run every tool with these credentials (the assumed investigator role)."""
    global _session
    _session = session
    _clients.clear()


def client(service):
    if service not in _clients:
        _clients[service] = _session.client(service)
    return _clients[service]


def compact(value):
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if k != "ResponseMetadata"}
    text = json.dumps(value, default=str, separators=(",", ":"))
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"... [truncated, {len(text)} chars total; narrow the request]"
    return text


@tool
def aws_read(service: str, operation: str, params: Optional[dict] = None) -> str:
    """Call any read-only AWS API: Describe*, List*, Get*, Lookup*, Filter*, Search*.

    service is the boto3 service name (lambda, sqs, dynamodb, apigatewayv2, events,
    iam, logs, cloudwatch, cloudtrail, resourcegroupstaggingapi, ...). operation is the
    API name in PascalCase as in the AWS docs, e.g. GetFunctionConfiguration,
    ListVersionsByFunction, GetQueueAttributes, GetStage, DescribeRule, GetRolePolicy.
    Data and secrets (S3 objects, table items, queue messages, secret values,
    function code) are not readable."""
    check_read(service, operation, params or {})
    return compact(hide_env_error(getattr(client(service), xform_name(operation))(**(params or {}))))


def hide_env_error(res):
    """The investigator role cannot decrypt Lambda environment variables (they may hold
    secrets), so Lambda returns an AccessDenied error in their place. Say what it is,
    so it is not mistaken for a fault in the function."""
    configs = [res] + [c for c in res.get("Versions", []) + res.get("Functions", []) if isinstance(c, dict)]
    for c in configs:
        env = c.get("Environment")
        if isinstance(env, dict) and env.get("Error"):
            c["Environment"] = {"note": "values hidden from the investigator role on purpose; "
                                        "not an error in the function. Use logs to see if a variable is missing."}
    return res


@tool
def logs_insights(log_group: str, query: str, minutes: int = 30) -> str:
    """Run a CloudWatch Logs Insights query over the last `minutes` minutes.

    Lambda log groups are /aws/lambda/<function-name>. Example query:
    fields @timestamp, @message | filter @message like /ERROR|Task timed out|Runtime/ | sort @timestamp desc | limit 20"""
    logs = client("logs")
    end = int(time.time())
    q = logs.start_query(logGroupName=log_group, startTime=end - minutes * 60, endTime=end,
                         queryString=query, limit=50)
    for _ in range(30):
        res = logs.get_query_results(queryId=q["queryId"])
        if res["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        time.sleep(1)
    rows = [{f["field"]: f["value"] for f in row if f["field"] != "@ptr"} for row in res.get("results", [])]
    return compact({"status": res["status"], "rows": rows})


@tool
def metric(namespace: str, metric_name: str, dimensions: dict, stat: str = "Sum",
           minutes: int = 30, period: int = 60) -> str:
    """Get one CloudWatch metric as a time series, oldest first.

    Examples: AWS/Lambda Errors {"FunctionName": "demo-orders-worker"};
    AWS/Lambda Duration with stat Maximum; AWS/SQS ApproximateNumberOfMessagesVisible
    {"QueueName": "..."}; AWS/ApiGateway 4xx {"ApiId": "...", "Stage": "$default"}."""
    end = datetime.now(timezone.utc)
    res = client("cloudwatch").get_metric_data(
        MetricDataQueries=[{
            "Id": "m",
            "MetricStat": {
                "Metric": {"Namespace": namespace, "MetricName": metric_name,
                           "Dimensions": [{"Name": k, "Value": v} for k, v in dimensions.items()]},
                "Period": period, "Stat": stat,
            },
        }],
        StartTime=end - timedelta(minutes=minutes), EndTime=end, ScanBy="TimestampAscending",
    )
    r = res["MetricDataResults"][0]
    points = [[t.strftime("%H:%M"), round(v, 3)] for t, v in zip(r["Timestamps"], r["Values"])]
    return compact({"unit_note": "missing minutes had no data", "points": points})


@tool
def recent_changes(minutes: int = 120, resource_name: Optional[str] = None) -> str:
    """Write events from CloudTrail: who changed what, and when.

    CloudTrail usually lags 5 to 15 minutes behind, so a missing event is not proof
    that nothing changed. Check LastModified fields in configs as well."""
    kwargs = {"StartTime": datetime.now(timezone.utc) - timedelta(minutes=minutes),
              "LookupAttributes": [{"AttributeKey": "ReadOnly", "AttributeValue": "false"}],
              "MaxResults": 50}
    if resource_name:
        kwargs["LookupAttributes"] = [{"AttributeKey": "ResourceName", "AttributeValue": resource_name}]
    events = []
    for e in client("cloudtrail").lookup_events(**kwargs)["Events"]:
        detail = json.loads(e.get("CloudTrailEvent", "{}"))
        if detail.get("readOnly"):
            continue
        events.append({
            "time": str(e["EventTime"]),
            "event": e["EventName"],
            "source": detail.get("eventSource"),
            "by": detail.get("userIdentity", {}).get("arn"),
            "resources": [r.get("ResourceName") for r in e.get("Resources", [])],
            "request": detail.get("requestParameters"),
            "error": detail.get("errorCode"),
        })
    return compact({"events": events})


READ_TOOLS = [aws_read, logs_insights, metric, recent_changes]


class Evidence(BaseModel):
    claim: str = Field(description="One factual statement")
    refs: list[str] = Field(description="IDs of the tool results that show it, e.g. ['R3', 'R7']")


class RuledOut(BaseModel):
    hypothesis: str
    reason: str
    refs: list[str] = Field(default_factory=list)


class Call(BaseModel):
    service: str = Field(description="boto3 service name, e.g. lambda")
    operation: str = Field(description="PascalCase API name, e.g. UpdateAlias")
    params: dict = Field(description="Exact API parameters")


class Proposal(BaseModel):
    call: Call
    undo: Optional[Call] = Field(None, description="The call that restores the state you read before the change")
    irreversible: bool = False
    expected_effect: str = Field(description="What should change in the metrics, and how soon")


class SubmitArgs(BaseModel):
    action: Literal["propose", "advise", "escalate"] = Field(
        description="propose: one write the executor may run. advise: the fix needs something "
                    "outside your bounds, so describe it for a human. escalate: the evidence does not "
                    "show a cause.")
    summary: str = Field(description="Two or three sentences for the on-call engineer")
    root_cause: str
    evidence: list[Evidence]
    ruled_out: list[RuledOut] = Field(default_factory=list)
    proposal: Optional[Proposal] = None
    advice: Optional[str] = Field(None, description="For advise: the exact change a human should make")


@tool("submit", args_schema=SubmitArgs)
def submit(**kwargs) -> str:
    """Finish the investigation with a diagnosis and at most one proposed write."""
    return "received"
