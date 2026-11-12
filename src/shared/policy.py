"""What the agent may read and which writes the executor will run.

This is the second of three layers. The first is the agent's IAM role
(ReadOnlyAccess with data reads denied). The third is the executor's IAM role
and permission boundary (writes only to resources tagged oncall=managed). If
this file has a gap, the IAM layers still refuse the call.
"""

import os

import botocore.session
from botocore.exceptions import DataNotFoundError, UnknownServiceError
from botocore.model import OperationNotFoundError
from botocore.validate import ParamValidator

SYSTEM_PREFIX = os.environ.get("SYSTEM_PREFIX", "oncall-")

READ_PREFIXES = ("Describe", "List", "Get", "Lookup", "Filter", "Search")
# Reads that do not follow the naming pattern.
EXTRA_READS = {("logs", "StartQuery"), ("logs", "StopQuery")}

# Reads that return data or secrets rather than configuration or behaviour.
# "*" means every operation of that service.
DATA_READS = {
    "s3": {"GetObject", "GetObjectAttributes", "GetObjectTorrent", "SelectObjectContent"},
    "dynamodb": {"GetItem", "BatchGetItem", "Query", "Scan", "ExecuteStatement", "BatchExecuteStatement"},
    "dynamodbstreams": {"GetRecords"},
    "kinesis": {"GetRecords"},
    "sqs": {"ReceiveMessage"},
    "lambda": {"GetFunction"},  # returns a download link for the code
    "ssm": {"GetParameter", "GetParameters", "GetParametersByPath", "GetParameterHistory"},
    "secretsmanager": "*",
    "kms": "*",
}

# Services the executor never writes to: identity, keys, billing, audit, the
# alarms themselves (silencing an alarm is not a fix) and the on-call system.
DENIED_WRITE_SERVICES = {
    "iam", "kms", "sts", "organizations", "account", "billing", "budgets", "ce", "cur",
    "cloudtrail", "config", "guardduty", "securityhub", "sso", "sso-admin", "identitystore",
    "secretsmanager", "ssm", "acm", "cloudwatch", "states", "support",
}

DENIED_WRITE_PREFIXES = (
    "Delete", "Remove", "Terminate", "Detach", "Revoke", "Deregister",
    "Create", "Run", "Copy", "Restore", "Import",
    "Tag", "Untag", "Invoke", "Publish", "Send", "Purge", "Reset", "Flush",
)

# Matches a denied prefix but only removes a limit, so it stays allowed.
ALLOWED_DESPITE_PREFIX = {
    ("lambda", "DeleteFunctionConcurrency"),
}

DENIED_WRITE_OPERATIONS = {
    ("lambda", "UpdateFunctionCode"),
    ("lambda", "AddPermission"),
    ("lambda", "UpdateFunctionUrlConfig"),
    ("s3", "PutObject"),
    ("s3", "PutObjectAcl"),
    ("s3", "PutBucketAcl"),
    ("dynamodb", "PutItem"),
    ("dynamodb", "UpdateItem"),
    ("dynamodb", "BatchWriteItem"),
    ("dynamodb", "TransactWriteItems"),
    ("dynamodb", "ExecuteStatement"),
}

# Parameters that change who can do what. A write carrying any of these is
# refused wherever it appears, e.g. a queue policy inside SetQueueAttributes or
# a new execution role inside UpdateFunctionConfiguration.
DENIED_PARAM_KEYS = {
    "Policy", "PolicyDocument", "PolicyText", "ResourcePolicy",
    "Role", "RoleArn", "ExecutionRoleArn", "KMSKeyArn", "KmsKeyId",
}

_session = botocore.session.get_session()


class PolicyError(Exception):
    pass


def _operation_model(service, operation):
    try:
        return _session.get_service_model(service).operation_model(operation)
    except (UnknownServiceError, DataNotFoundError):
        raise PolicyError(f"unknown service '{service}'")
    except OperationNotFoundError:
        raise PolicyError(f"'{service}' has no operation '{operation}' (use the PascalCase API name)")


def _param_problems(params, op_model):
    report = ParamValidator().validate(params or {}, op_model.input_shape)
    return [report.generate_report()] if report.has_errors() else []


def is_read(service, operation):
    return operation.startswith(READ_PREFIXES) or (service, operation) in EXTRA_READS


def check_read(service, operation, params):
    """Raise PolicyError unless this is a permitted configuration read."""
    op_model = _operation_model(service, operation)
    if not is_read(service, operation):
        raise PolicyError(f"{service} {operation} is not a read; the agent can only propose writes")
    denied = DATA_READS.get(service, set())
    if denied == "*" or operation in denied:
        raise PolicyError(f"{service} {operation} reads data or secrets, which the agent may not see")
    problems = _param_problems(params, op_model)
    if problems:
        raise PolicyError("; ".join(problems))
    return op_model


def _walk(value, path=""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield f"{path}.{k}" if path else k, k, v
            yield from _walk(v, f"{path}.{k}" if path else k)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]")


def write_problems(call):
    """Every reason the executor would refuse this call. Empty means allowed."""
    if not isinstance(call, dict):
        return ["call must be an object with service, operation and params"]
    service, operation = call.get("service"), call.get("operation")
    params = call.get("params") or {}
    if not service or not operation:
        return ["call needs both service and operation"]
    try:
        op_model = _operation_model(service, operation)
    except PolicyError as e:
        return [str(e)]

    problems = []
    if is_read(service, operation):
        problems.append(f"{service} {operation} is a read; use aws_read instead of proposing it")
    if service in DENIED_WRITE_SERVICES:
        problems.append(f"writes to {service} are never allowed")
    if operation.startswith(DENIED_WRITE_PREFIXES) and (service, operation) not in ALLOWED_DESPITE_PREFIX:
        problems.append(f"{operation}: deletes, creates, tags, invocations and sends are never allowed")
    if (service, operation) in DENIED_WRITE_OPERATIONS:
        problems.append(f"{service} {operation} is never allowed (code, data or access changes)")
    for path, key, v in _walk(params):
        if key in DENIED_PARAM_KEYS:
            problems.append(f"parameter {path} changes access or encryption, which is never allowed")
        if isinstance(v, str) and SYSTEM_PREFIX in v:
            problems.append(f"parameter {path} refers to the on-call system itself ({v})")
    problems += _param_problems(params, op_model)
    return problems


def proposal_problems(proposal):
    """Problems with a full proposal: the call, its undo and the bookkeeping."""
    if not isinstance(proposal, dict):
        return ["proposal is missing"]
    problems = [f"call: {p}" for p in write_problems(proposal.get("call"))]
    if proposal.get("irreversible"):
        if proposal.get("undo"):
            problems.append("an irreversible proposal must not carry an undo call")
    elif not proposal.get("undo"):
        problems.append("undo: give the call that restores the current state, or mark irreversible")
    else:
        problems += [f"undo: {p}" for p in write_problems(proposal.get("undo"))]
    return problems
