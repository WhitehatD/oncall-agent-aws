"""demo-orders-worker: takes orders off the queue, renders the invoice, charges the card."""

import json
import os
import time

import boto3

table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])

INVOICE_MB = 100          # the invoice is rendered in memory
PAYMENT_LATENCY_S = 1.5   # the payment provider is slow


def handler(event, _context):
    for record in event["Records"]:
        msg = json.loads(record["body"])
        items = msg["items"]
        if not isinstance(items, list):
            raise ValueError(f"order {msg.get('order_id')} has malformed items: {items!r}")
        invoice = bytearray(INVOICE_MB * 1024 * 1024)
        for i in range(0, len(invoice), 4096):
            invoice[i] = 1
        time.sleep(PAYMENT_LATENCY_S)
        table.update_item(Key={"order_id": msg["order_id"]}, UpdateExpression="SET #s = :s",
                          ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "paid"})
        print(json.dumps({"paid": msg["order_id"], "items": len(items)}))
