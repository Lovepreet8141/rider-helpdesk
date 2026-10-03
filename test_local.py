"""Local end-to-end test — no real Intercom or MotionTools needed (Intercom runs in dry run).

    python3 test_local.py            # runs the checks
    python3 test_local.py --serve    # same data, then opens the dashboard on http://127.0.0.1:8031/help  (password: test)
"""
import hashlib
import hmac
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["DASHBOARD_PASSWORD"] = "test"
os.environ["WEBHOOK_PATH_SECRET"] = "s3cret"
os.environ["INTERCOM_CLIENT_SECRET"] = "icsecret"
for k in ("INTERCOM_TOKEN", "MT_API_TOKEN"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402

import app as A  # noqa: E402

c = TestClient(A.app)
AUTH = ("ops", "test")
UTC = timezone.utc
NOW = datetime.now(UTC)
MUC, HAM, AAC = "area-muc", "area-ham", "area-aac"
iso = lambda m: (NOW - timedelta(minutes=m)).isoformat()   # noqa: E731


def mt(resource, event, data, mins_ago=0):
    r = c.post("/mt/s3cret", json={"resource_type": resource, "event": event, "timestamp": iso(mins_ago), "data": data})
    assert r.status_code == 200, r.text


def ic(topic, conv, contact_id, name, text, part="p1", sig=True):
    if topic == "conversation.user.created":
        item = {"type": "conversation", "id": conv, "source": {"id": part, "body": f"<p>{text}</p>",
                "author": {"type": "user", "id": contact_id, "name": name}},
                "contacts": {"contacts": [{"type": "contact", "id": contact_id}]}}
    else:
        item = {"type": "conversation", "id": conv, "contacts": {"contacts": [{"type": "contact", "id": contact_id}]},
                "conversation_parts": {"conversation_parts": [{"id": part, "part_type": "comment", "body": f"<p>{text}</p>",
                                                               "author": {"type": "user", "id": contact_id, "name": name}}]}}
    body = json.dumps({"type": "notification_event", "topic": topic, "id": f"n-{conv}-{part}", "data": {"item": item}}).encode()
    s = "sha1=" + hmac.new(b"icsecret", body, hashlib.sha1).hexdigest() if sig else "sha1=bad"
    return c.post("/intercom/s3cret", content=body, headers={"X-Hub-Signature": s, "Content-Type": "application/json"})


def last(conv):
    return A.store.handled(1, conversation_id=conv)[0]


# ---------------------------------------------------------------- MotionTools evening
for rid, first, last_, phone, area in [("d-ahmed", "Ahmed", "Fauzi", "+49 176 12345678", MUC),
                                       ("d-karan", "Karan", "Singh", "+49 176 11122233", HAM),
                                       ("d-baraa", "Baraa", "", "+49 152 99988877", AAC)]:
    mt("driver", "online", {"driver_id": rid, "service_area_id": area,
                            "profile": {"first_name": first, "last_name": last_, "phone_number": phone}}, 60)

# Ahmed (Munich): order WPC4W7, at the restaurant for 6 min
mt("booking", "created", {"booking_id": "b-1", "external_id": "WPC4W7", "service_area_id": MUC, "status": "pickable", "place_ids": ["pl-1"]}, 20)
mt("tour", "created", {"tour_id": "t-1", "dispatched_booking_ids": ["b-1"], "status": "pickable"}, 20)
mt("tour", "transition", {"tour_id": "t-1", "to": "claimed", "affected_user_ids": ["d-ahmed"]}, 18)
mt("booking", "in_progress", {"booking_id": "b-1", "driver_id": "d-ahmed", "driver_name": "Ahmed Fauzi"}, 16)
mt("booking", "etas_recalculated", {"booking_id": "b-1", "unfinished_stops_info": [
    {"id": "s1", "type": "pickup", "eta": iso(7)}, {"id": "s2", "type": "dropoff", "eta": iso(-14)}]}, 15)
mt("booking", "stop_arrived", {"booking_id": "b-1", "driver_id": "d-ahmed", "stop_type": "pickup", "stop_id": "s1"}, 6)

# Karan (Hamburg): order HH77Q2 at the customer for 3 min
mt("booking", "created", {"booking_id": "b-2", "external_id": "HH77Q2", "service_area_id": HAM, "status": "pickable", "place_ids": ["pl-2"]}, 35)
mt("booking", "transition", {"booking_id": "b-2", "to": "claimed", "affected_user_ids": ["d-karan"]}, 33)
mt("booking", "in_progress", {"booking_id": "b-2", "driver_id": "d-karan", "driver_name": "Karan Singh"}, 32)
mt("booking", "stop_completed", {"booking_id": "b-2", "driver_id": "d-karan", "stop_type": "pickup"}, 20)
mt("booking", "stop_arrived", {"booking_id": "b-2", "driver_id": "d-karan", "stop_type": "dropoff"}, 3)

# second order for Karan, delivered earlier
mt("booking", "created", {"booking_id": "b-3", "external_id": "HH11AA", "service_area_id": HAM, "status": "pickable"}, 90)
mt("booking", "transition", {"booking_id": "b-3", "to": "claimed", "affected_user_ids": ["d-karan"]}, 88)
mt("booking", "stop_completed", {"booking_id": "b-3", "driver_id": "d-karan", "stop_type": "dropoff"}, 60)

A.store.put("area_names", {MUC: "Munich", HAM: "Hamburg", AAC: "Aachen"})
A.store.put("place_names", {"pl-1": "Burger Palast", "pl-2": "Pho Hanoi"})

# ---------------------------------------------------------------- Intercom messages
checks = []


def check(name, cond, info=""):
    checks.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   -> {info}"))


# 1. Karan (matched by name) — customer not answering -> "team fetches number" + escalated, note has order & city
r = ic("conversation.user.created", "c-1", "ct-karan", "Karan Singh", "customer not answering, pls send number")
h = last("c-1")
check("Karan: customer unreachable detected", h["intent"] == "customer_unreachable", h)
check("Karan: matched to MotionTools rider", h["rider_id"] == "d-karan", h)
check("Karan: right order (at customer)", h["order_ref"] == "HH77Q2", h)
check("Karan: replied + escalated", "auto-reply" in h["action"] and "escalated" in h["action"], h["action"])
check("Karan: note shows Hamburg + 'at customer 3 min'", "Hamburg" in h["note"] and "at customer 3 min" in h["note"], h["note"])
check("Karan: contact auto-linked", (A.store.linked("ct-karan") or {}).get("rider_id") == "d-karan")

# 2. Ahmed — German, food not ready -> restaurant wait reply with minutes, no escalation
ic("conversation.user.created", "c-2", "ct-ahmed", "Ahmed Fauzi", "Essen ist noch nicht fertig, ich warte")
h = last("c-2")
check("Ahmed: restaurant wait", h["intent"] == "restaurant_wait", h)
check("Ahmed: German reply with restaurant + minutes", "Burger Palast" in h["reply"] and "6 Min" in h["reply"], h["reply"])
check("Ahmed: solved by bot (not escalated)", h["action"] == "auto-reply", h["action"])

# 3. Ahmed asks the same again 1 min later -> bot doesn't repeat, team gets it
ic("conversation.user.replied", "c-2", "ct-ahmed", "Ahmed Fauzi", "still waiting, food not ready", part="p2")
h = last("c-2")
check("Ahmed repeat: escalated, no second auto reply", "escalated" in h["action"] and "auto-reply" not in h["action"], h)

# 4. Unknown contact, but sends Ahmed's order number -> identified by order
ic("conversation.user.created", "c-3", "ct-new", "Phone 2", "Bestellung WPC4W7 welche Bestellung habe ich?")
h = last("c-3")
check("Order number identifies rider", h["rider_id"] == "d-ahmed" and h["order_ref"] == "WPC4W7", h)
check("Status reply lists the order", "WPC4W7" in h["reply"], h["reply"])

# 5. Completely unknown rider: customer problem -> ask for order number + escalate
ic("conversation.user.created", "c-4", "ct-x", "Someone", "nobody opens the door")
h = last("c-4")
check("Unknown rider: asks for order number + escalates", "order number" in h["reply"] and "escalated" in h["action"], h)

# 6. Duplicate webhook delivery -> ignored
n = len(A.store.handled(500))
r = ic("conversation.user.created", "c-4", "ct-x", "Someone", "nobody opens the door")
check("Duplicate webhook ignored", r.json().get("duplicate") and len(A.store.handled(500)) == n, r.text)

# 7. Accident -> urgent
ic("conversation.user.created", "c-5", "ct-baraa", "Baraa", "I had an accident with the bike")
h = last("c-5")
check("Accident: urgent escalation + 112 reply", "URGENT" in h["action"] and "112" in h["reply"], h)

# 8. Wrong signature -> rejected
r = ic("conversation.user.created", "c-6", "ct-x", "Someone", "hi", sig=False)
check("Bad signature rejected", r.status_code == 401, r.status_code)

# 9. Teammate already answering -> bot quiet, still posts the note
async def fake_conv(cid):
    return {"conversation_parts": {"conversation_parts": [{"id": "human-1", "part_type": "comment",
            "author": {"type": "admin", "id": "99"}, "created_at": int(NOW.timestamp()) - 120}]}}
orig = A.ic.conversation
A.ic.conversation = fake_conv
ic("conversation.user.replied", "c-1", "ct-karan", "Karan Singh", "which order do I have now?", part="p9")
A.ic.conversation = orig
h = last("c-1")
check("Human in chat: bot silent", "auto-reply" not in h["action"], h["action"])

# 10. Thanks -> nothing
ic("conversation.user.replied", "c-2", "ct-ahmed", "Ahmed Fauzi", "ok danke", part="p3")
h = last("c-2")
check("Thanks: no reply, no escalation", h["intent"] == "thanks" and h["action"].startswith("silent"), h)

# 11. Dashboard + preview endpoint
s = c.get("/api/state", auth=AUTH).json()
check("Dashboard state loads", s["today"]["total"] >= 8 and len(s["riders"]) == 3, s["today"])
p = c.post("/api/test", auth=AUTH, json={"text": "app not working cant swipe", "rider_id": "d-karan"}).json()
check("Preview: app problem first steps", p["intent"] == "app_problem" and "Close the app" in p["reply"], p)
check("Dashboard needs password", c.get("/api/state").status_code == 401)
check("Dry-run outbox has the Intercom calls", len(A.ic.outbox) >= 10, len(A.ic.outbox))

ok = sum(x[1] for x in checks)
print(f"\n{ok}/{len(checks)} checks passed")
print("\n--- example: note posted for Karan ---\n" + A.store.handled(50, conversation_id="c-1")[-1]["note"])

if "--serve" in sys.argv:
    import uvicorn
    uvicorn.run(A.app, host="127.0.0.1", port=8031)
elif ok != len(checks):
    sys.exit(1)
