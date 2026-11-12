"""demo-orders-api: POST /orders saves an order and queues it for payment."""

import json
import os
import time
import uuid
from decimal import Decimal

import boto3

table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
sqs = boto3.client("sqs")


def handler(event, _context):
    if event.get("rawPath") == "/health":
        return {"statusCode": 200, "body": "ok"}
    order = json.loads(event.get("body") or "{}")
    items = order["items"]
    total = sum(i["price"] * i["qty"] for i in items)  # CHAOS:TOTAL
    order_id = str(uuid.uuid4())
    table.put_item(Item={"order_id": order_id, "status": "received",
                         "total": Decimal(str(round(total, 2))), "created_at": int(time.time())})
    sqs.send_message(QueueUrl=os.environ["QUEUE_URL"],
                     MessageBody=json.dumps({"order_id": order_id, "items": items}))
    return {"statusCode": 201, "body": json.dumps({"order_id": order_id})}
