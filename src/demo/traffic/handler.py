"""demo-traffic: places a few orders every minute so there are always metrics."""

import json
import os
import random
import urllib.error
import urllib.request


def handler(_event, _context):
    statuses = []
    for _ in range(int(os.environ.get("ORDERS_PER_RUN", "3"))):
        body = json.dumps({"items": [{"sku": random.choice(["tea", "mug", "pot"]), "price": 4.5,
                                      "qty": random.randint(1, 3)}]}).encode()
        req = urllib.request.Request(os.environ["API_URL"].rstrip("/") + "/orders", data=body, method="POST",
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                statuses.append(res.status)
        except urllib.error.HTTPError as e:
            statuses.append(e.code)
        except Exception as e:  # noqa: BLE001
            statuses.append(type(e).__name__)
    print(json.dumps({"statuses": statuses}))
