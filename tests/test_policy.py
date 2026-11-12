import pytest

from shared.policy import PolicyError, check_read, proposal_problems, write_problems


def call(service, operation, **params):
    return {"service": service, "operation": operation, "params": params}


ALLOWED = [
    call("lambda", "UpdateAlias", FunctionName="demo-orders-api", Name="live", FunctionVersion="3"),
    call("lambda", "UpdateFunctionConfiguration", FunctionName="demo-orders-worker", Timeout=15),
    call("lambda", "UpdateFunctionConfiguration", FunctionName="demo-orders-worker", MemorySize=512),
    call("lambda", "PutFunctionConcurrency", FunctionName="demo-orders-api", ReservedConcurrentExecutions=10),
    call("lambda", "DeleteFunctionConcurrency", FunctionName="demo-orders-api"),
    call("lambda", "UpdateEventSourceMapping", UUID="a1b2c3d4-0000-4000-8000-123456789abc", Enabled=True),
    call("dynamodb", "UpdateTable", TableName="demo-orders",
         ProvisionedThroughput={"ReadCapacityUnits": 10, "WriteCapacityUnits": 10}),
    call("events", "EnableRule", Name="demo-traffic-schedule"),
    call("sqs", "SetQueueAttributes", QueueUrl="https://sqs/x/demo-orders", Attributes={"VisibilityTimeout": "120"}),
    call("sqs", "StartMessageMoveTask", SourceArn="arn:aws:sqs:eu-central-1:1:demo-orders-dlq"),
]

REFUSED = [
    (call("iam", "PutRolePolicy", RoleName="r", PolicyName="p", PolicyDocument="{}"), "iam"),
    (call("lambda", "DeleteFunction", FunctionName="demo-orders-api"), "deletes"),
    (call("lambda", "UpdateFunctionCode", FunctionName="demo-orders-api", ZipFile=b"x"), "code"),
    (call("lambda", "PublishVersion", FunctionName="demo-orders-api"), "deletes"),
    (call("lambda", "Invoke", FunctionName="demo-orders-api"), "invocations"),
    (call("dynamodb", "PutItem", TableName="demo-orders", Item={}), "never allowed"),
    (call("sqs", "PurgeQueue", QueueUrl="q"), "deletes"),
    (call("cloudwatch", "DisableAlarmActions", AlarmNames=["demo-api-errors"]), "cloudwatch"),
    (call("lambda", "UpdateFunctionConfiguration", FunctionName="demo-orders-worker", Role="arn:aws:iam::1:role/admin"),
     "access"),
    (call("sqs", "SetQueueAttributes", QueueUrl="q", Attributes={"Policy": "{}"}), "access"),
    (call("lambda", "UpdateFunctionConfiguration", FunctionName="oncall-executor", Timeout=30), "on-call system"),
    (call("lambda", "UpdateFunctionConfiguration", FunctionName="demo-orders-worker", Timeout="ten"), "Invalid type"),
    (call("lambda", "UpdateFunctionConfiguration", Timeout=15), "Missing required"),
    (call("lambda", "MakeItWork", FunctionName="x"), "no operation"),
    (call("nosuchservice", "Foo"), "unknown service"),
    (call("lambda", "GetFunctionConfiguration", FunctionName="x"), "is a read"),
]


@pytest.mark.parametrize("c", ALLOWED, ids=lambda c: c["operation"])
def test_allowed_writes(c):
    assert write_problems(c) == []


@pytest.mark.parametrize("c,why", REFUSED, ids=lambda x: x["operation"] if isinstance(x, dict) else "")
def test_refused_writes(c, why):
    problems = write_problems(c)
    assert problems
    if why:
        assert any(why in p for p in problems), problems


def test_proposal_needs_undo():
    good = call("lambda", "UpdateAlias", FunctionName="demo-orders-api", Name="live", FunctionVersion="3")
    undo = call("lambda", "UpdateAlias", FunctionName="demo-orders-api", Name="live", FunctionVersion="4")
    assert proposal_problems({"call": good, "undo": undo}) == []
    assert any("undo" in p for p in proposal_problems({"call": good}))
    assert proposal_problems({"call": good, "irreversible": True}) == []
    bad_undo = call("lambda", "DeleteFunction", FunctionName="demo-orders-api")
    assert any(p.startswith("undo:") for p in proposal_problems({"call": good, "undo": bad_undo}))


@pytest.mark.parametrize("service,op", [
    ("lambda", "GetFunctionConfiguration"), ("lambda", "ListVersionsByFunction"),
    ("sqs", "GetQueueAttributes"), ("iam", "GetRolePolicy"), ("events", "DescribeRule"),
])
def test_config_reads_allowed(service, op):
    params = {"lambda": {"FunctionName": "x"}, "sqs": {"QueueUrl": "q"},
              "iam": {"RoleName": "r", "PolicyName": "p"}, "events": {"Name": "r"}}[service]
    check_read(service, op, params)


@pytest.mark.parametrize("service,op,params", [
    ("s3", "GetObject", {"Bucket": "b", "Key": "k"}),
    ("dynamodb", "Scan", {"TableName": "t"}),
    ("sqs", "ReceiveMessage", {"QueueUrl": "q"}),
    ("lambda", "GetFunction", {"FunctionName": "x"}),
    ("ssm", "GetParameter", {"Name": "/oncall/openrouter-api-key"}),
    ("secretsmanager", "ListSecrets", {}),
    ("lambda", "UpdateAlias", {"FunctionName": "x", "Name": "live"}),
])
def test_data_and_writes_not_readable(service, op, params):
    with pytest.raises(PolicyError):
        check_read(service, op, params)
