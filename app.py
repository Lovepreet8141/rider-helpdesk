"""Quickzi Rider Helpdesk — Intercom ↔ MotionTools.

Riders write to Quickzi in Intercom.  This service
  1. keeps a live picture of every order and rider from the MotionTools webhooks (all cities),
  2. receives every new rider message from an Intercom webhook,
  3. works out who the rider is, which order they're on and what they're asking,
  4. answers the simple ones in the chat right away (status, waiting at restaurant, app first-steps, …),
     and for the rest replies "team is on it", posts an internal note with the full order context and
     assigns the chat to the ops team.

Separate from the Quickzi Ops dashboard (own repo, own Railway service, own database).

Env:  INTERCOM_TOKEN, INTERCOM_REGION (us|eu|au), INTERCOM_ADMIN_ID (optional), INTERCOM_CLIENT_SECRET (optional,
      verifies webhook signatures), MT_API_TOKEN (optional), DASHBOARD_PASSWORD, WEBHOOK_PATH_SECRET, DATA_DIR (/data)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import brain
from intercom import Intercom, signature_ok, strip_html
from mt import MotionTools
from store import Store
from tracker import Tracker, phase_of, ts

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("helpdesk")
UTC = timezone.utc
VERSION = "1.0"


def env(k, d=""):
    return (os.environ.get(k) or d).strip()


DATA_DIR = Path(env("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DASH_PASSWORD = env("DASHBOARD_PASSWORD")
PATH_SECRET = env("WEBHOOK_PATH_SECRET", "change-me")
IC_SECRET = env("INTERCOM_CLIENT_SECRET")

store = Store(str(DATA_DIR / "helpdesk.db"))
tracker = Tracker(store)
ic = Intercom(env("INTERCOM_TOKEN"), env("INTERCOM_REGION", "us"), env("INTERCOM_ADMIN_ID"))
mt = MotionTools(env("MT_API_TOKEN"))
app = FastAPI(title="Quickzi Rider Helpdesk")
basic = HTTPBasic()
STARTED = datetime.now(UTC)
STATS = {"mt_events": 0, "mt_rejected": 0, "ic_events": 0, "ic_rejected": 0, "ic_last": None, "errors": [], "detail_fetch": 0}
CONV_LOCKS: dict = {}


def require_login(creds: HTTPBasicCredentials = Depends(basic)):
    if not DASH_PASSWORD or not secrets.compare_digest(creds.password.strip().encode(), DASH_PASSWORD.encode()):
        raise HTTPException(401, "Wrong password", headers={"WWW-Authenticate": "Basic"})


def settings() -> dict:
    s = dict(brain.DEFAULT_SETTINGS)
    s.update(store.get("settings", {}) or {})
    return s


def templates() -> dict:
    t = {k: dict(v) for k, v in brain.DEFAULT_TEMPLATES.items()}
    for k, v in (store.get("templates", {}) or {}).items():
        if k in t and isinstance(v, dict):
            t[k].update({lang: txt for lang, txt in v.items() if txt and txt.strip()})
    return t


def err(msg: str):
    log.warning(msg)
    STATS["errors"].insert(0, {"at": datetime.now(UTC).isoformat(timespec="seconds"), "msg": msg[:400]})
    del STATS["errors"][30:]


# ====================================================================== rider matching
def digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def same_phone(a, b) -> bool:
    a, b = digits(a), digits(b)
    return len(a) >= 8 and len(b) >= 8 and a[-9:] == b[-9:]


def same_name(a, b) -> bool:
    a, b = brain.norm(a), brain.norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    ta, tb = set(a.split()), set(b.split())
    return len(ta) >= 2 and len(tb) >= 2 and (ta <= tb or tb <= ta)


def identify(contact: dict, text: str):
    """-> (rider dict | None, how, named_order | None)"""
    named = None
    for code in brain.order_codes(text):
        named = tracker.find_order(code)
        if named:
            break
    cid = contact.get("id") or ""
    link = store.linked(cid) if cid else None
    if link and link["rider_id"] in tracker.riders:
        return tracker.riders[link["rider_id"]], f"saved link ({link['how']})", named
    if named and named.get("rider_id"):
        r = tracker.rider(named["rider_id"])
        if cid:
            store.link(cid, r["id"], "order number", contact.get("name") or "")
        return r, "order number in message", named
    if contact.get("phone"):
        hits = [r for r in tracker.riders.values() if same_phone(r.get("phone"), contact["phone"])]
        if len(hits) == 1:
            if cid:
                store.link(cid, hits[0]["id"], "phone", contact.get("name") or "")
            return hits[0], "phone number", named
    if contact.get("name"):
        hits = [r for r in tracker.riders.values() if same_name(r.get("name"), contact["name"])]
        if len(hits) == 1:
            if cid:
                store.link(cid, hits[0]["id"], "name", contact.get("name") or "")
            return hits[0], "name", named
    return None, "", named


def restaurant_name(place_id):
    return tracker.restaurant(place_id)


def detail_state() -> str:
    if not mt.enabled:
        return "no MotionTools token set"
    st = [mt.stats["endpoints"].get(k) for k in ("/api/bookings/{id}", "/api/hailing/bookings/{id}")]
    st = [x for x in st if x]
    return "booking detail: " + (", ".join(st) if st else "not tried yet")


async def customer_contact(o: dict):
    """Customer phone/address for an order — only possible if MotionTools lets this token read one booking."""
    if not o:
        return "", ""
    if o.get("customer_phone") or o.get("customer_addr"):
        return o.get("customer_phone", ""), o.get("customer_addr", "")
    if not mt.enabled or not mt.detail_available():
        return "", ""
    STATS["detail_fetch"] += 1
    b = await mt.get_booking(o["id"])
    if not b:
        return "", ""
    stops = b.get("stops") or []
    drops = [s for s in stops if s.get("type") == "dropoff"]
    pick = next((s for s in stops if s.get("type") == "pickup"), {})
    drop = drops[-1] if drops else {}
    street = " ".join(x for x in [drop.get("street"), str(drop.get("number") or "")] if x).strip()
    o["customer_phone"] = drop.get("phone_number") or ""
    o["customer_addr"] = ", ".join(x for x in [street, drop.get("zip_code") and str(drop.get("zip_code")), drop.get("city")] if x)
    o["restaurant_phone"] = pick.get("phone_number") or ""
    pname = (pick.get("place") or {}).get("name")
    if pname and o.get("place_id") and not tracker.restaurant(o["place_id"]):
        names = store.get("place_names", {}) or {}
        names[o["place_id"]] = pname
        store.put("place_names", names)
    store.save_order(o)
    return o["customer_phone"], o["customer_addr"]


# ====================================================================== Intercom conversation helpers
def bot_parts() -> set:
    return set(store.get("bot_parts", []) or [])


def remember_bot_part(resp):
    try:
        parts = ((resp or {}).get("conversation_parts") or {}).get("conversation_parts") or []
        if parts:
            ids = (store.get("bot_parts", []) or []) + [str(parts[-1].get("id"))]
            store.put("bot_parts", ids[-2000:])
    except Exception:  # noqa: BLE001
        pass


async def human_recently(cid: str, quiet_min: int) -> bool:
    conv = await ic.conversation(cid)
    if not conv:
        return False
    mine = bot_parts()
    cut = datetime.now(UTC) - timedelta(minutes=quiet_min)
    for p in ((conv.get("conversation_parts") or {}).get("conversation_parts") or []):
        a = p.get("author") or {}
        if a.get("type") == "admin" and p.get("part_type") == "comment" and str(p.get("id")) not in mine:
            if ts(p.get("created_at")) and ts(p.get("created_at")) >= cut:
                return True
    return False


def parse_intercom(payload: dict):
    """-> dict(conversation_id, contact{id,name,email,phone}, text, part_id) or None"""
    topic = payload.get("topic") or ""
    item = ((payload.get("data") or {}).get("item")) or {}
    if item.get("type") != "conversation":
        return None
    cid = str(item.get("id") or "")
    contacts = ((item.get("contacts") or {}).get("contacts")) or []
    contact = {"id": str((contacts[0] if contacts else {}).get("id") or "")}
    text, part_id, author = "", "", {}
    if topic.endswith("user.created") or topic == "conversation.user.created":
        src = item.get("source") or {}
        text, part_id, author = strip_html(src.get("body") or ""), "src-" + str(src.get("id") or cid), src.get("author") or {}
    else:
        parts = ((item.get("conversation_parts") or {}).get("conversation_parts")) or []
        parts = [p for p in parts if (p.get("author") or {}).get("type") in ("user", "lead", "contact")]
        if parts:
            p = parts[-1]
            text, part_id, author = strip_html(p.get("body") or ""), str(p.get("id") or ""), p.get("author") or {}
    contact["name"] = author.get("name") or ""
    contact["email"] = author.get("email") or ""
    if not contact["id"] and author.get("id"):
        contact["id"] = str(author["id"])
    return {"conversation_id": cid, "contact": contact, "text": text, "part_id": part_id, "topic": topic}


# ====================================================================== the main flow
async def handle(msg: dict, send: bool = True):
    cid, contact, text = msg["conversation_id"], msg["contact"], (msg["text"] or "").strip()
    lock = CONV_LOCKS.setdefault(cid, asyncio.Lock())
    async with lock:
        if not text:
            store.log(conversation_id=cid, contact_id=contact.get("id"), contact_name=contact.get("name"),
                      intent="attachment", action="silent (photo/location only)", message="[attachment]")
            return None
        if contact.get("id") and not contact.get("phone") and not ic.dry:
            c = await ic.contact(contact["id"])
            if c:
                contact["phone"] = c.get("phone") or ""
                contact["name"] = contact.get("name") or c.get("name") or ""
        s, tpls = settings(), templates()
        rider, how, named = identify(contact, text)
        live = tracker.live_orders_of(rider["id"]) if rider else []
        recent = tracker.recent_orders_of(rider["id"]) if rider else []
        intent = brain.detect(text)[0]
        cust_phone, cust_addr = "", ""
        if intent in ("customer_unreachable", "address_problem"):
            o = brain.pick_order(intent, live, named) or (recent[0] if recent else None)
            try:
                cust_phone, cust_addr = await customer_contact(o)
            except Exception as e:  # noqa: BLE001
                err(f"customer lookup failed: {e}")
        history = store.handled(20, conversation_id=cid)
        human = await human_recently(cid, s["human_quiet_min"]) if send else False
        d = brain.decide(text, rider=rider, how=how, live=live, recent=recent, named_order=named,
                         customer_phone=cust_phone, customer_addr=cust_addr, restaurant_name=restaurant_name,
                         city_name=tracker.city, settings=s, templates=tpls, history=history, human_recent=human,
                         detail_state=detail_state(), contact_name=contact.get("name") or "")
        if not send:
            return d
        actions = []
        if d.reply:
            resp = await ic.reply(cid, d.reply)
            if resp is not None:
                remember_bot_part(resp)
                actions.append("auto-reply")
            else:
                actions.append("reply FAILED")
        if d.escalate or (s.get("post_note") and d.intent not in ("thanks", "greeting")):
            resp = await ic.note(cid, d.note)
            remember_bot_part(resp)
        if d.escalate:
            who = (s.get("urgent_assignee") or s.get("escalate_assignee")) if d.urgent else s.get("escalate_assignee")
            if who:
                await ic.assign(cid, who)
            if s.get("escalate_tag"):
                await ic.tag(cid, s["escalate_tag"])
            actions.append("escalated" + (" URGENT" if d.urgent else ""))
        if not actions:
            actions.append("silent" + (f" ({d.reason})" if d.reason else ""))
        store.log(conversation_id=cid, contact_id=contact.get("id"), contact_name=contact.get("name"),
                  rider_id=(rider or {}).get("id"), rider=(rider or {}).get("name"), order_ref=d.order_ref,
                  city=tracker.city((rider or {}).get("area")), intent=d.intent, action=" + ".join(actions),
                  message=text[:1000], reply=d.reply, note=d.note, lang=d.lang)
        return d


# ====================================================================== webhooks
@app.post("/mt/{secret}")
async def mt_webhook(secret: str, request: Request):
    if not secrets.compare_digest(secret, PATH_SECRET):
        STATS["mt_rejected"] += 1
        raise HTTPException(404)
    try:
        body = await request.json()
    except ValueError:
        return {"ok": False}
    for p in (body if isinstance(body, list) else [body]):
        if isinstance(p, dict):
            try:
                tracker.apply(p)
                STATS["mt_events"] += 1
            except Exception as e:  # noqa: BLE001
                err(f"MotionTools event failed: {e}")
    return {"ok": True}


@app.post("/intercom/{secret}")
async def intercom_webhook(secret: str, request: Request, bg: BackgroundTasks):
    if not secrets.compare_digest(secret, PATH_SECRET):
        STATS["ic_rejected"] += 1
        raise HTTPException(404)
    raw = await request.body()
    if not signature_ok(IC_SECRET, raw, request.headers.get("X-Hub-Signature", "")):
        STATS["ic_rejected"] += 1
        err("Intercom webhook with a wrong signature (check INTERCOM_CLIENT_SECRET)")
        raise HTTPException(401)
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return {"ok": False}
    STATS["ic_events"] += 1
    STATS["ic_last"] = datetime.now(UTC).isoformat(timespec="seconds")
    if payload.get("topic") == "ping":
        return {"ok": True, "pong": True}
    msg = parse_intercom(payload)
    if not msg or not msg["conversation_id"]:
        return {"ok": True, "ignored": True}
    key = f"ic:{msg['conversation_id']}:{msg['part_id'] or payload.get('id')}"
    if not store.first_time(key):
        return {"ok": True, "duplicate": True}

    async def run():
        try:
            await handle(msg)
        except Exception as e:  # noqa: BLE001
            err(f"handling conversation {msg['conversation_id']} failed: {e}")
    bg.add_task(run)
    return {"ok": True}


# ====================================================================== dashboard API
@app.get("/")
async def root():
    return RedirectResponse("/help")


@app.get("/help", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def dashboard():
    return HTMLResponse((Path(__file__).parent / "help.html").read_text(encoding="utf-8"))


@app.get("/health")
async def health():
    return {"ok": True, "version": VERSION, "mt_events": STATS["mt_events"], "ic_events": STATS["ic_events"],
            "last_mt_event": tracker.last_event, "intercom": "dry run" if ic.dry else "live"}


def rider_row(r: dict) -> dict:
    live = tracker.live_orders_of(r["id"])
    return {"id": r["id"], "name": r.get("name") or "", "phone": r.get("phone") or "", "online": r.get("online"),
            "city": tracker.city(r.get("area")), "area": r.get("area"), "last_seen": r.get("last_seen"),
            "live": [{"ref": o.get("ref"), "phase": phase_of(o)} for o in live]}


@app.get("/api/state", dependencies=[Depends(require_login)])
async def state():
    now = datetime.now(UTC)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    riders = sorted((rider_row(r) for r in tracker.riders.values()),
                    key=lambda r: (not r["live"], not r["online"], r["name"].lower()))
    areas = sorted({str(o.get("area")) for o in tracker.orders.values() if o.get("area")} |
                   {str(r.get("area")) for r in tracker.riders.values() if r.get("area")})
    places = sorted({str(o.get("place_id")) for o in tracker.orders.values() if o.get("place_id")})
    return {
        "version": VERSION, "now": now.isoformat(timespec="seconds"),
        "today": store.stats_since(day - timedelta(hours=2)), "week": store.stats_since(now - timedelta(days=7)),
        "handled": store.handled(300), "riders": riders, "links": store.links(),
        "settings": settings(), "templates": templates(), "template_labels": brain.TEMPLATE_LABEL,
        "intent_labels": brain.INTENT_LABEL,
        "area_names": store.get("area_names", {}) or {}, "place_names": store.get("place_names", {}) or {},
        "areas_seen": areas, "places_seen": places[-300:],
        "system": {
            "intercom": "DRY RUN — no INTERCOM_TOKEN set, nothing is sent" if ic.dry else "live",
            "intercom_me": ic.stats.get("me"), "intercom_admin_id": ic.admin_id, "intercom_errors": ic.stats["errors"],
            "intercom_last_error": ic.stats["last_error"], "signature_check": bool(IC_SECRET),
            "mt_events": STATS["mt_events"], "mt_event_types": tracker.counts, "last_mt_event": tracker.last_event,
            "orders_tracked": len(tracker.orders),
            "live_orders": sum(1 for o in tracker.orders.values() if phase_of(o) in ("waiting", "accepted", "to_restaurant", "at_restaurant", "to_customer", "at_customer")),
            "riders_known": len(tracker.riders), "ic_events": STATS["ic_events"], "ic_last": STATS["ic_last"],
            "rejected": {"mt": STATS["mt_rejected"], "intercom": STATS["ic_rejected"]},
            "mt_token": mt.enabled, "mt_endpoints": mt.endpoint_summary(), "detail_fetches": STATS["detail_fetch"],
            "errors": STATS["errors"], "started": STARTED.isoformat(timespec="seconds"),
            "outbox": list(ic.outbox)[:40],
        },
    }


@app.post("/api/settings", dependencies=[Depends(require_login)])
async def save_settings(request: Request):
    body = await request.json()
    if isinstance(body.get("settings"), dict):
        cur = store.get("settings", {}) or {}
        for k, v in body["settings"].items():
            if k in brain.DEFAULT_SETTINGS:
                default = brain.DEFAULT_SETTINGS[k]
                if isinstance(default, bool):
                    v = bool(v)
                elif isinstance(default, int):
                    try:
                        v = int(v)
                    except (TypeError, ValueError):
                        continue
                else:
                    v = str(v or "").strip()
                cur[k] = v
        store.put("settings", cur)
    if isinstance(body.get("templates"), dict):
        cur = store.get("templates", {}) or {}
        for k, v in body["templates"].items():
            if k in brain.DEFAULT_TEMPLATES and isinstance(v, dict):
                cur[k] = {lang: str(v.get(lang) or "") for lang in ("en", "de")}
        store.put("templates", cur)
    for key in ("area_names", "place_names"):
        if isinstance(body.get(key), dict):
            store.put(key, {str(k).strip(): str(v).strip() for k, v in body[key].items() if str(k).strip() and str(v).strip()})
    return {"ok": True}


@app.post("/api/link", dependencies=[Depends(require_login)])
async def save_link(request: Request):
    b = await request.json()
    cid = str(b.get("contact_id") or "").strip()
    if not cid:
        raise HTTPException(400, "contact_id missing")
    store.link(cid, str(b.get("rider_id") or "").strip(), "set by hand", str(b.get("contact_name") or ""))
    return {"ok": True}


@app.post("/api/test", dependencies=[Depends(require_login)])
async def test_message(request: Request):
    """Preview: what would the bot answer to this message from this rider? Nothing is sent."""
    b = await request.json()
    rid = str(b.get("rider_id") or "")
    contact = {"id": "", "name": b.get("contact_name") or "", "phone": b.get("contact_phone") or ""}
    if rid and rid in tracker.riders:
        contact["name"] = tracker.riders[rid].get("name") or contact["name"]
        contact["phone"] = tracker.riders[rid].get("phone") or contact["phone"]
    d = await handle({"conversation_id": "preview", "contact": contact, "text": b.get("text") or ""}, send=False)
    if d is None:
        return {"intent": "attachment", "reply": "", "note": "", "escalate": False}
    return {"intent": d.intent, "intent_label": brain.INTENT_LABEL.get(d.intent, d.intent), "lang": d.lang,
            "template": d.template, "reply": d.reply, "note": d.note, "escalate": d.escalate, "urgent": d.urgent,
            "reason": d.reason, "order": d.order_ref, "scores": d.scores}


@app.post("/api/probe", dependencies=[Depends(require_login)])
async def probe():
    if not mt.enabled:
        return {"ok": False, "summary": "no MT_API_TOKEN set"}
    bid = next((o["id"] for o in sorted(tracker.orders.values(), key=lambda o: o.get("last_event_at") or "", reverse=True)), None)
    await mt.probe(booking_id=bid)
    return {"ok": True, "summary": mt.endpoint_summary()}


@app.get("/api/conversation/{cid}", dependencies=[Depends(require_login)])
async def conversation_rows(cid: str):
    return store.handled(100, conversation_id=cid)


# ====================================================================== background
async def housekeeping():
    while True:
        try:
            tracker.prune()
            store.prune()
            for cid in [c for c, lk in CONV_LOCKS.items() if not lk.locked()][:-200]:
                CONV_LOCKS.pop(cid, None)
        except Exception as e:  # noqa: BLE001
            err(f"housekeeping: {e}")
        await asyncio.sleep(3600)


@app.on_event("startup")
async def startup():
    if not ic.dry:
        await ic.me()
        if not ic.admin_id:
            err("Intercom: could not read the admin id — set INTERCOM_ADMIN_ID")
    asyncio.create_task(housekeeping())
    log.info("Rider helpdesk %s up — Intercom %s, MotionTools token %s", VERSION, "dry run" if ic.dry else "live",
             "set" if mt.enabled else "not set")
