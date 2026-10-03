"""Robot test against a fake MotionTools server: sign-in, token expiry, booking lookup -> customer number to the rider.

    python3 test_robot.py
"""
import asyncio
import json
import os
import tempfile

import httpx

os.environ.update(DATA_DIR=tempfile.mkdtemp(), DASHBOARD_PASSWORD="test", WEBHOOK_PATH_SECRET="s3cret",
                  MT_ROBOT_EMAIL="robot@quickzi.test", MT_ROBOT_PASSWORD="pw")
for k in ("INTERCOM_TOKEN", "INTERCOM_CLIENT_SECRET", "MT_API_TOKEN"):
    os.environ.pop(k, None)

CALLS = []
STATE = {"token": "tok-1", "n": 0}


def fake(req: httpx.Request):
    CALLS.append(f"{req.method} {req.url.path}")
    assert req.headers.get("X-Client-Version"), "X-Client-Version header missing"
    if req.url.path == "/api/signin":
        body = json.loads(req.content)
        assert body == {"user": {"email": "robot@quickzi.test", "password": "pw"}}
        STATE["n"] += 1
        STATE["token"] = f"tok-{STATE['n']}"
        return httpx.Response(200, json={"access_token": STATE["token"], "refresh_token": "r1", "expires_in": 7200})
    if req.headers.get("Authorization") != f"Bearer {STATE['token']}":
        return httpx.Response(401, json={"error_code": "unauthorized"})
    if req.url.path == "/api/hailing/bookings/b-2":
        return httpx.Response(200, json={"booking": {"id": "b-2", "external_id": "HH77Q2", "status": "en_route", "stops": [
            {"type": "pickup", "location_name": "Pho Hanoi", "phone_number": "+4940111"},
            {"type": "dropoff", "street": "Dosseweg", "number": "18", "zip_code": "22547", "city": "Hamburg",
             "first_name": "Chris", "last_name": "D.", "phone_number": "+491760000000"}]}})
    return httpx.Response(404, json={"error_code": "not_found"})


_Orig = httpx.AsyncClient


class Mocked(_Orig):
    def __init__(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(fake)
        super().__init__(*a, **kw)


httpx.AsyncClient = Mocked

from fastapi.testclient import TestClient  # noqa: E402

import app as A  # noqa: E402

ok = []


def check(name, cond, info=""):
    ok.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   -> {info}"))


with TestClient(A.app) as c:            # runs startup -> robot signs in
    check("Robot signed in at startup", A.mt.robot["signed_in"], A.mt.robot)
    for p in [{"resource_type": "driver", "event": "online", "data": {"driver_id": "d-karan", "service_area_id": "ham",
               "profile": {"first_name": "Karan", "last_name": "Singh", "phone_number": "+49 176 11122233"}}},
              {"resource_type": "booking", "event": "created", "data": {"booking_id": "b-2", "external_id": "HH77Q2", "status": "pickable"}},
              {"resource_type": "booking", "event": "in_progress", "data": {"booking_id": "b-2", "driver_id": "d-karan"}},
              {"resource_type": "booking", "event": "stop_arrived", "data": {"booking_id": "b-2", "driver_id": "d-karan", "stop_type": "dropoff"}}]:
        c.post("/mt/s3cret", json=p)

    STATE["token"] = "rotated-on-server"         # simulate the session expiring on MotionTools' side
    body = {"topic": "conversation.user.created", "id": "n1", "data": {"item": {"type": "conversation", "id": "c-1",
            "source": {"id": "p1", "body": "<p>customer not answering</p>", "author": {"type": "user", "id": "ct-k", "name": "Karan Singh"}},
            "contacts": {"contacts": [{"id": "ct-k"}]}}}}
    c.post("/intercom/s3cret", json=body)
    h = A.store.handled(1, conversation_id="c-1")[0]
    check("Customer number sent to the rider", "+491760000000" in h["reply"] and h["intent"] == "customer_unreachable", h["reply"])
    check("Solved without the team", h["action"] == "auto-reply", h["action"])
    check("Note has the address", "Dosseweg 18" in h["note"], h["note"])
    check("Robot signed in again after the session expired", STATE["n"] == 2, CALLS)

    # second question on the same order -> no new MotionTools call (cached on the order)
    n = len(CALLS)
    body["id"], body["data"]["item"]["id"], body["data"]["item"]["source"]["id"] = "n2", "c-2", "p2"
    c.post("/intercom/s3cret", json=body)
    check("Cached: no second lookup for the same order", len(CALLS) == n, CALLS[n:])
    s = c.get("/api/state", auth=("ops", "test")).json()["system"]
    check("Dashboard shows robot status", s["robot"]["signed_in"] and s["robot"]["lookups"] == 1, s["robot"])

print(f"\n{sum(ok)}/{len(ok)} checks passed")
print("MotionTools calls:", CALLS)
