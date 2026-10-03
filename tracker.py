"""Live picture of orders and riders, rebuilt from MotionTools webhook events (restricted API mode).

Same event mapping as the Quickzi Ops dashboard (events.py), cut down to what a rider question needs:
which orders a rider has right now, what stage each is in, how long they've been at the restaurant / customer,
and the ETAs.  All cities are kept (Munich, Hamburg, Hamburg-Harburg, Aachen …); the city name comes from the
service-area id → name table in Settings.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc
DISPATCHED = {"pickable", "claimed", "en_route"}
DONE = {"done", "completed", "finished", "paid", "processing_payment"}
LIVE_PHASES = ("waiting", "accepted", "to_restaurant", "at_restaurant", "to_customer", "at_customer")
PHASE_TEXT = {
    "on_hold": ("on hold (not released yet)", "noch nicht freigegeben"),
    "waiting": ("waiting for a rider", "wartet auf Fahrer"),
    "accepted": ("accepted, not started", "angenommen, noch nicht gestartet"),
    "to_restaurant": ("riding to the restaurant", "auf dem Weg zum Restaurant"),
    "at_restaurant": ("at the restaurant", "im Restaurant"),
    "to_customer": ("delivering to the customer", "auf dem Weg zum Kunden"),
    "at_customer": ("at the customer", "beim Kunden"),
    "delivered": ("delivered", "zugestellt"),
    "cancelled": ("cancelled", "storniert"),
}


def ts(v):
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, UTC)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        return None


def iso(d):
    return d.isoformat(timespec="seconds") if isinstance(d, datetime) else d


def new_order(bid: str, ref: str = "", area=None) -> dict:
    return {"id": bid, "ref": ref or bid[:8], "area": area, "status": "", "phase": "waiting", "rider_id": None,
            "rider": "", "place_id": "", "restaurant": "", "tour_id": None, "cancelled": False, "riders_before": [],
            "created_at": None, "dispatched_at": None, "accepted_at": None, "started_at": None,
            "at_restaurant_at": None, "picked_up_at": None, "at_customer_at": None, "delivered_at": None,
            "eta_restaurant": None, "eta_customer": None, "promised_at": None, "last_event_at": None,
            "customer_phone": "", "customer_addr": "", "restaurant_phone": "", "detail_at": None, "stop_types": {}}


def phase_of(o: dict) -> str:
    if o.get("cancelled"):
        return "cancelled"
    if o.get("delivered_at"):
        return "delivered"
    if not o.get("rider_id"):
        return "waiting" if o.get("dispatched_at") else "on_hold"
    if o.get("at_customer_at"):
        return "at_customer"
    if o.get("picked_up_at"):
        return "to_customer"
    if o.get("at_restaurant_at"):
        return "at_restaurant"
    if o.get("started_at"):
        return "to_restaurant"
    return "accepted"


class Tracker:
    def __init__(self, store):
        self.store = store
        self.orders: dict = {}
        self.riders: dict = {}
        self.tours: dict = self.store.get("tours", {}) or {}
        self.counts: dict = {}
        self.last_event = None
        self.freed: list = []          # (order_id, rider_id) — order left this rider (hand-back / redispatch / cancel)
        for o in store.load("orders", since_hours=36):
            self.orders[o["id"]] = o
        for r in store.load_all_riders():
            self.riders[r["id"]] = r

    # ---------------- names ----------------
    def city(self, area) -> str:
        names = self.store.get("area_names", {}) or {}
        return names.get(str(area or ""), "") if area else ""

    def restaurant(self, place_id) -> str:
        names = self.store.get("place_names", {}) or {}
        return names.get(str(place_id or ""), "")

    # ---------------- helpers ----------------
    def rider(self, rid: str) -> dict:
        r = self.riders.get(rid)
        if not r:
            r = self.riders[rid] = {"id": rid, "name": "", "phone": "", "online": False, "last_seen": None, "area": None}
        return r

    def set_rider(self, o: dict, rid, name, now):
        if not rid:
            return
        r = self.rider(rid)
        if name and not r["name"]:
            r["name"] = name
        if o.get("rider_id") and o["rider_id"] != rid:
            self.freed.append((o["id"], o["rider_id"]))
            o["riders_before"].append(o["rider_id"])
            for k in ("accepted_at", "started_at", "at_restaurant_at"):
                o[k] = None
        o["rider_id"] = rid
        o["rider"] = r["name"] or name or o.get("rider") or ""
        o["accepted_at"] = o.get("accepted_at") or iso(now)
        r["last_seen"] = iso(now)
        r["online"] = True
        if o.get("area"):
            r["area"] = o["area"]
        self.store.save_rider(r)

    def handback(self, o: dict):
        if o.get("rider_id"):
            self.freed.append((o["id"], o["rider_id"]))
            o["riders_before"].append(o["rider_id"])
        o["rider_id"], o["rider"] = None, ""
        for k in ("accepted_at", "started_at", "at_restaurant_at"):
            o[k] = None

    def order(self, bid: str, d: dict) -> dict:
        o = self.orders.get(bid)
        if not o:
            o = self.orders[bid] = new_order(bid, d.get("external_id") or "", d.get("service_area_id"))
        if d.get("external_id"):
            o["ref"] = d["external_id"]
        if d.get("service_area_id"):
            o["area"] = d["service_area_id"]
        return o

    def done(self, o: dict, now: datetime):
        o["phase"] = phase_of(o)
        o["last_event_at"] = iso(now)
        self.store.save_order(o)

    # ---------------- the event switch ----------------
    def apply(self, p: dict) -> str:
        rtype, ev = str(p.get("resource_type") or ""), str(p.get("event") or "")
        name = f"{rtype}.{ev}"
        d = p.get("data") or {}
        now = ts(p.get("timestamp") or d.get("timestamp")) or datetime.now(UTC)
        self.counts[name] = self.counts.get(name, 0) + 1
        self.last_event = iso(datetime.now(UTC))

        if rtype == "booking":
            bid = d.get("booking_id")
            if not bid:
                return "no booking id"
            o = self.order(bid, d)
            if ev == "created":
                o["created_at"] = o.get("created_at") or iso(now)
                o["status"] = d.get("status") or o["status"]
                if o["status"] in DISPATCHED:
                    o["dispatched_at"] = o.get("dispatched_at") or iso(now)
                pids = d.get("place_ids") or []
                pids = [pids] if isinstance(pids, str) else pids
                if pids:
                    o["place_id"] = pids[0]
            elif ev == "transition":
                to = str(d.get("to") or "")
                if to in DONE:
                    o["delivered_at"] = o.get("delivered_at") or iso(now)
                elif to == "cancelled":
                    o["cancelled"] = True
                    if o.get("rider_id"):
                        self.freed.append((o["id"], o["rider_id"]))
                elif to in DISPATCHED:
                    o["dispatched_at"] = o.get("dispatched_at") or iso(now)
                    if to == "pickable" and o.get("rider_id") and not o.get("picked_up_at"):
                        self.handback(o)
                    users = d.get("affected_user_ids") or []
                    users = [users] if isinstance(users, str) else list(users)
                    if to in ("claimed", "en_route") and users:
                        self.set_rider(o, users[0], "", now)
                    if to == "en_route" and o.get("rider_id"):
                        o["started_at"] = o.get("started_at") or iso(now)
                elif to == "to_be_dispatched" and o.get("rider_id") and not o.get("picked_up_at"):
                    self.handback(o)
                o["status"] = to or o["status"]
            elif ev == "in_progress":
                o["dispatched_at"] = o.get("dispatched_at") or iso(now)
                self.set_rider(o, d.get("driver_id"), d.get("driver_name"), now)
                o["started_at"] = o.get("started_at") or iso(now)
            elif ev == "etas_recalculated":
                for s in d.get("unfinished_stops_info") or []:
                    kind = "pickup" if "pick" in str(s.get("type", "")).lower() else "dropoff"
                    o["stop_types"][str(s.get("id"))] = kind
                    eta = ts(s.get("eta"))
                    if eta:
                        o["eta_restaurant" if kind == "pickup" else "eta_customer"] = iso(eta)
                        if kind == "dropoff" and not o.get("promised_at"):
                            o["promised_at"] = iso(eta)
            elif ev in ("stop_arrived", "stop_completed", "stop_failed"):
                o["dispatched_at"] = o.get("dispatched_at") or iso(now)
                self.set_rider(o, d.get("driver_id"), d.get("driver_name"), now)
                kind = str(d.get("stop_type") or o["stop_types"].get(str(d.get("stop_id")), "")).lower()
                if kind in ("task", "return"):
                    pass
                elif ev == "stop_arrived":
                    key = "at_restaurant_at" if kind == "pickup" else "at_customer_at"
                    o[key] = o.get(key) or iso(now)
                elif ev == "stop_completed":
                    if kind == "pickup":
                        o["picked_up_at"] = o.get("picked_up_at") or iso(now)
                        o["at_restaurant_at"] = o.get("at_restaurant_at") or iso(now)
                    else:
                        o["at_customer_at"] = o.get("at_customer_at") or iso(now)
                        o["delivered_at"] = o.get("delivered_at") or iso(now)
                elif ev == "stop_failed" and kind != "pickup":
                    o["cancelled"] = True
            elif ev == "driver_location_updated":
                return name                                   # GPS is not needed here
            self.done(o, now)
            return name

        if rtype == "driver":
            rid = d.get("driver_id")
            if not rid:
                return "no driver id"
            r = self.rider(rid)
            prof = d.get("profile") or {}
            pname = " ".join(x for x in [prof.get("first_name"), prof.get("last_name")] if x).strip()
            if pname:
                r["name"] = pname
            if prof.get("phone_number"):
                r["phone"] = prof["phone_number"]
            if d.get("service_area_id"):
                r["area"] = d["service_area_id"]
            r["last_seen"] = iso(now)
            if ev == "online" or ev in ("busy", "no_longer_busy"):
                r["online"] = True
            elif ev == "offline":
                r["online"] = False
            self.store.save_rider(r)
            return name

        if rtype == "tour":
            tid = d.get("tour_id")
            if ev == "created":
                ids = d.get("dispatched_booking_ids") or []
                self.tours[tid] = [ids] if isinstance(ids, str) else list(ids)
                self.store.put("tours", dict(list(self.tours.items())[-3000:]))
                return name
            if ev == "transition":
                to = d.get("to")
                users = d.get("affected_user_ids") or []
                users = [users] if isinstance(users, str) else users
                for bid in self.tours.get(tid, []):
                    o = self.orders.get(bid)
                    if not o:
                        continue
                    o["tour_id"] = tid
                    if to == "claimed" and users:
                        o["dispatched_at"] = o.get("dispatched_at") or iso(now)
                        self.set_rider(o, users[0], "", now)
                    elif to in ("pickable",) and o.get("rider_id") and not o.get("picked_up_at"):
                        self.handback(o)
                    self.done(o, now)
            return name
        return "ignored"

    # ---------------- questions ----------------
    def live_orders_of(self, rid: str) -> list:
        out = [o for o in self.orders.values() if o.get("rider_id") == rid and phase_of(o) in LIVE_PHASES]
        rank = {p: i for i, p in enumerate(reversed(LIVE_PHASES))}     # at_customer first
        return sorted(out, key=lambda o: (rank.get(phase_of(o), 9), o.get("eta_customer") or ""))

    def recent_orders_of(self, rid: str, hours: int = 2) -> list:
        cut = datetime.now(UTC) - timedelta(hours=hours)
        out = [o for o in self.orders.values() if o.get("rider_id") == rid and phase_of(o) not in LIVE_PHASES
               and (ts(o.get("last_event_at")) or cut) >= cut]
        return sorted(out, key=lambda o: o.get("last_event_at") or "", reverse=True)

    def find_order(self, token: str):
        t = token.strip().lower().lstrip("#")
        if len(t) < 4:
            return None
        for o in self.orders.values():
            if t in (str(o.get("ref") or "").lower(), str(o["id"]).lower(), str(o["id"])[:8].lower()):
                return o
        return None

    def prune(self):
        cut = datetime.now(UTC) - timedelta(hours=36)
        for bid in [b for b, o in self.orders.items() if (ts(o.get("last_event_at")) or ts(o.get("created_at")) or cut) < cut]:
            self.orders.pop(bid, None)
