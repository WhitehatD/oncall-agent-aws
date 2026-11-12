"""Print the latest incident's status and events, then follow it until it waits on you or ends.

    python scripts/watch.py           # follow the latest incident
    python scripts/watch.py --once    # print it once
"""

import json
import sys
import time
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Attr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import chaos  # noqa: E402
from shared import store  # noqa: E402

STOP = {"awaiting_approval", "resolved", "advised", "escalated", "timeout", "failed"}


def latest(table):
    items = table.scan(FilterExpression=Attr("pk").begins_with("INC#"))["Items"]
    return max(items, key=lambda i: i["created_at"])["id"] if items else None


def show(event):
    d = event["data"]
    t = time.strftime("%H:%M:%S", time.localtime(int(event["t"])))
    if event["kind"] == "diagnosis":
        print(f"{t} diagnosis: {d['action']} after {d['tool_calls']} tool calls")
        for step in d.get("trace", []):
            if "id" in step:
                print(f"           [{step['id']}] {step['tool']} {json.dumps(step['args'])[:150]}{'' if step['ok'] else '  (refused/failed)'}")
            else:
                print(f"           check: {step['check']} {step.get('problems', '')}")
    else:
        print(f"{t} {event['kind']}: {json.dumps(d, default=str)[:200]}")


def main():
    out = chaos.tf_outputs()
    store._table = boto3.resource("dynamodb", region_name=out["region"]).Table(out["incidents_table"])
    incident_id, seen = latest(store._table), 0
    if not incident_id:
        sys.exit("no incidents yet")
    while True:
        inc = store.get(incident_id)
        for e in inc["events"][seen:]:
            show(e)
        seen = len(inc["events"])
        if "--once" in sys.argv or inc["status"] in STOP:
            break
        time.sleep(10)
    print(f"\nincident {inc['id']}: {inc['status']}, alarms {inc['alarms']}, "
          f"model cost ${inc['cost_usd']:.4f}, {inc['tokens']} tokens")
    diag = inc.get("diagnosis") or {}
    if diag:
        print("\nsummary:   ", diag.get("summary"))
        print("root cause:", diag.get("root_cause"))
        for ev in diag.get("evidence", []):
            print(f"  - {ev['claim']} {ev.get('refs')}")
        for r in diag.get("ruled_out", []):
            print(f"  ruled out: {r['hypothesis']}: {r['reason']}")
        if inc.get("proposal"):
            print("\nproposal:", json.dumps(inc["proposal"], indent=1))
        if diag.get("advice"):
            print("\nadvice:", diag["advice"])


if __name__ == "__main__":
    main()
