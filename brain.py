"""What did the rider ask, and what do we answer?  Pure functions — no I/O — so it is easy to test.

detect(text)            -> (intent, lang)
pick_order(intent, …)   -> the order the question is most likely about
decide(…)               -> Decision: reply text (or None), escalate?, urgent?, internal note for the team
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from tracker import PHASE_TEXT, phase_of, ts

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")


def norm(s: str) -> str:
    s = (s or "").lower().replace("’", "'").replace("ß", "ss")
    s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip()


C = r"(customer|costumer|custumer|client|kunde\w*|kundin|grahak)"
# (intent, weight, pattern) — every pattern that matches adds its weight; the highest score wins.
RULES = [
    # --- customer can't be reached / needs the customer's number
    ("customer_unreachable", 3, C + r".{0,40}\b(reach|answer\w*|respond\w*|pick\w* up|picks up|open\w*|home|there|here|available|"
                                    r"not coming|erreich\w*|ran|antwort\w*|macht nicht auf|oeffnet nicht|nicht da|zuhause|zu hause|meldet|"
                                    r"utha|nahi|nhi)"),
    ("customer_unreachable", 3, r"\b(reach|call|contact|erreich\w*|anruf\w*|anrufen)\b.{0,20}" + C),
    ("customer_unreachable", 3, r"\b(nobody|no one|noone|no body|niemand|keiner)\b.{0,25}\b(open\w*|answer\w*|there|home|macht|da|auf|antwort\w*|ran)"),
    ("customer_unreachable", 4, C + r"\W{0,3}(s )?(phone|number|nummer|telefon\w*|tel|handy|no\.?)\b"),
    ("customer_unreachable", 4, r"\b(phone|number|nummer|telefon\w*|handynummer)\b.{0,20}(of the |vom |des )?" + C),
    ("customer_unreachable", 2, r"\b(doorbell|klingel\w*|ring\w* the bell|bell)\b"),
    ("customer_unreachable", 3, r"(الزبون|العميل|زبون).{0,30}(يرد|يجاوب|رقم|موجود)|رقم (الزبون|العميل)"),
    # --- address problems
    ("address_problem", 3, r"\b(wrong|incorrect|falsch\w*)\b.{0,15}\b(address|adresse|addresse|location|haus\w*)"),
    ("address_problem", 3, r"\b(address|adresse|addresse)\b.{0,30}\b(wrong|not|incomplete|missing|falsch|nicht|fehlt|unvollstaendig|find\w*)"),
    ("address_problem", 3, r"\b(can'?t|cannot|can not|unable to|kann)\b.{0,25}\b(find|finde\w*)\b"),
    ("address_problem", 2, r"\b(house number|hausnummer|entrance|eingang|floor|etage|stockwerk|hinterhaus|building|gebaeude|apartment|wohnung)\b"),
    ("address_problem", 2, r"\bnicht (zu )?finden\b|\bfinde .{0,15}nicht\b"),
    # --- waiting at the restaurant / food not ready
    ("restaurant_wait", 3, r"\b(food|order|essen|bestellung|khana)\b.{0,30}\b(not ready|isn'?t ready|not done|nicht fertig|noch nicht|still|dauert|late|taking long|ready nahi)"),
    ("restaurant_wait", 3, r"\b(waiting|wait|warte\w*|wait kar)\b.{0,30}\b(restaurant|food|essen|order|bestellung|pickup|abholung|since|seit|min)"),
    ("restaurant_wait", 2, r"\b(not ready|nicht fertig|still cooking|kocht noch|dauert noch|taking too long|dauert zu lange|so long|so lange)\b"),
    ("restaurant_wait", 2, r"\brestaurant\b.{0,30}\b(slow|late|waiting|not ready|langsam|warte\w*|dauert)\b"),
    # --- restaurant has no order / closed
    ("restaurant_no_order", 4, r"\b(restaurant|they|staff|sie|die)\b.{0,40}\b(no order|don'?t have|doesn'?t have|have no|haben (die |keine |kein )?\w* ?nicht|"
                               r"keine bestellung|kein\w* order|not found|nicht gefunden|kennen .{0,15}nicht|didn'?t get|didn'?t receive|nicht bekommen)"),
    ("restaurant_no_order", 4, r"\b(restaurant|laden|shop|store)\b.{0,20}\b(closed|geschlossen|zu|is shut|band|bandh)\b"),
    ("restaurant_no_order", 3, r"\b(order|bestellung)\b.{0,20}\b(not there|nicht da|not in system|nicht im system|doesn'?t exist|existiert nicht)"),
    # --- food damaged / items missing / wrong order
    ("problem_food", 4, r"\b(spill\w*|damag\w*|broken|crushed|leak\w*|kaputt|ausgelaufen|verschuett\w*|beschaedigt|zerdrueckt)\b"),
    ("problem_food", 3, r"\b(missing|fehlt|fehlen|wrong (order|food|item|bag)|falsche (bestellung|tuete|essen)|drink|getraenk)\b"),
    # --- rider wants to hand the order back (be redispatched to another rider)
    ("handback", 6, r"\b(i|ich|me|main)\b.{0,25}\b(want|wanna|will|moechte|need to|have to|muss)\b.{0,20}\b(cancel|storn\w*|abgeben|give (it )?back|drop|nicht (mehr )?warten|not wait)"),
    ("handback", 5, r"\b(don'?t|do not|won'?t|not going to|can'?t|cannot)\b (want to |wanna )?wait\b"),
    ("handback", 5, r"\b(will|moechte|kann)\b (nicht|nicht mehr|keine lust) (mehr )?(warten|lange warten)"),
    ("handback", 6, r"\b(unassign\w*|reassign\w*|redispatch\w*|re-dispatch\w*|give (the |this |my )?order to (someone|somebody|another|other)|"
                    r"take (this |the |my )?order (away|off|from me|back)|remove (the |this |my )?order|abgeben|neu vergeben|anderen fahrer|jemand anderem|wait nahi)\b"),
    ("handback", 5, r"\b(please |pls |plz |bitte )?(cancel|stornier\w*) (my |this |the |meine |diese |die |den )?(order|bestellung|auftrag|tour)\b"),
    # --- cancelled / give back / what to do with the food
    ("cancel", 3, C + r".{0,20}\b(cancel\w*|storn\w*|abgesagt|doesn'?t want|will .{0,10}nicht)"),
    ("cancel", 4, r"\b(cancel\w*|storn\w*|abgesagt|refused|lehnt .{0,10}ab|doesn'?t want|will .{0,10}nicht)\b"),
    ("cancel", 3, r"\b(what (should|do) i do with|was (soll|mache) ich mit|bring (it )?back|zurueck ?bringen|keep the food|essen behalten)\b"),
    # --- accident / emergency / vehicle
    ("emergency", 6, r"\b(accident|unfall|crash\w*|injur\w*|verletz\w*|hurt|police|polizei|ambulance|krankenwagen|fell|gestuerzt|hingefallen|"
                     r"stolen|gestohlen|robbed|ueberfall\w*|attacked|angegriffen|hospital|krankenhaus)\b"),
    ("emergency", 4, r"\b(flat tire|flat tyre|platten|puncture|bike (is )?broken|fahrrad (ist )?kaputt|e-?bike|akku (ist )?leer|battery (is )?(dead|empty)|scooter)\b"),
    # --- app trouble
    ("app_problem", 4, r"\b(app|application|anwendung|system)\b.{0,40}\b(not work\w*|doesn'?t work|isn'?t working|stuck|crash\w*|frozen|freez\w*|hang\w*|bug|error|"
                       r"fehler|funktioniert nicht|geht nicht|haengt|laedt nicht|not loading|slow|logged out|ausgeloggt|kaam nahi)"),
    ("app_problem", 4, r"\b(can'?t|cannot|can not|unable to|kann)\b.{0,30}\b(swipe|slide|complete|finish|confirm|mark|click|press|abschliess\w*|bestaetig\w*|druecken|klicken|wischen)\b"),
    ("app_problem", 3, r"\b(swipe|slide|button)\b.{0,25}\b(not|doesn'?t|isn'?t|nicht|won'?t)\b"),
    ("app_problem", 3, r"\b(gps)\b"),
    # --- which order / status
    ("order_status", 3, r"\b(which order|my orders?|welche bestellung|meine bestellung\w*|next order|naechste\w* bestellung|order status|status)\b"),
    ("order_status", 2, r"\b(where (do|should) i (go|deliver)|wohin|how much time|how long (do|until)|wie lange (noch|habe)|eta|deadline)\b"),
    ("order_status", 2, r"\b(did i get|do i have|habe ich).{0,15}\b(order|bestellung)"),
    # --- shift / pay / admin (not live)
    ("shift_pay", 3, r"\b(shift|schicht\w*|salary|payment|paid|pay|payout|lohn|gehalt|money|geld|invoice|rechnung|abrechnung|vacation|urlaub|"
                     r"sick|krank\w*|day off|frei(en)? tag|contract|vertrag|tips?|trinkgeld|hours|stunden)\b"),
]
GREETING = re.compile(r"^(hi+|hello|hallo|hey|hai|salam|salaam|assalam\w*|servus|moin|guten (tag|abend|morgen)|good (evening|morning)|"
                      r"sat sri akal|namaste|yo|bro|help|hilfe)[\s!.,?]*$")
THANKS = re.compile(r"^((ok(ay)?|thanks?( you)?|thank u|thx|ty|danke( schoen| dir)?|vielen dank|alles klar|perfekt|perfect|super|"
                    r"got it|done|erledigt|shukriya|shukran|bro|👍|🙏)[\s!.,?👍🙏]*)+$")
ORDER_CODE = re.compile(r"(?<![\w-])#?([A-Za-z0-9]{5,12}|[0-9a-f]{8}-[0-9a-f-]{27,})(?![\w-])")

DE_WORDS = {"ich", "nicht", "der", "die", "das", "kunde", "kunden", "bestellung", "ist", "und", "bitte", "kein", "keine", "wo",
            "warte", "hallo", "geht", "macht", "auf", "noch", "essen", "mit", "was", "wie", "habe", "seit", "schon", "niemand",
            "adresse", "falsche", "hausnummer", "fertig", "geschlossen", "haengt", "danke", "bin", "hier", "kannst", "du", "mir"}
EN_WORDS = {"the", "not", "customer", "order", "is", "and", "please", "where", "i", "can't", "cant", "no", "answer", "waiting",
            "food", "hello", "with", "what", "how", "have", "since", "already", "nobody", "my", "it", "don't"}


def detect(text: str):
    t = norm(text)
    words = re.findall(r"[a-z']+", t)
    de = sum(w in DE_WORDS for w in words)
    en = sum(w in EN_WORDS for w in words)
    lang = "de" if de > en else "en"
    scores: dict = {}
    for intent, w, pat in RULES:
        if re.search(pat, t):
            scores[intent] = scores.get(intent, 0) + w
    if scores:
        best = max(scores.items(), key=lambda kv: (kv[1], -[r[0] for r in RULES].index(kv[0])))
        return best[0], lang, scores
    if THANKS.match(t):
        return "thanks", lang, {}
    if GREETING.match(t) or len(words) <= 2 and any(GREETING.match(w) for w in words):
        return "greeting", lang, {}
    return "unknown", lang, {}


def order_codes(text: str) -> list:
    out = []
    for m in ORDER_CODE.finditer(text or ""):
        tok = m.group(1)
        if tok.isalpha() and tok.lower() == tok:          # plain lowercase words are not codes
            continue
        out.append(tok)
    return out


# ------------------------------------------------------------------ templates
INTENT_LABEL = {
    "customer_unreachable": "Customer not reachable / needs customer number",
    "address_problem": "Address problem",
    "restaurant_wait": "Waiting at restaurant / food not ready",
    "restaurant_no_order": "Restaurant has no order / closed",
    "problem_food": "Food damaged / missing / wrong",
    "cancel": "Cancel / what to do with the food",
    "handback": "Rider wants to hand back the order (redispatch)",
    "handback_done": "Hand-back confirmed to rider",
    "emergency": "Accident / bike / emergency",
    "app_problem": "App problem",
    "order_status": "Which order / status",
    "shift_pay": "Shift / pay / admin",
    "greeting": "Greeting only",
    "thanks": "Thanks / ok",
    "unknown": "Not recognised",
}

DEFAULT_TEMPLATES = {
    "cust_phone": {
        "en": "Hi {name}, customer number for order {ref}: {phone}\nPlease call once more and wait 5 minutes at the door. If there's still no answer, write here — don't leave with the food.",
        "de": "Hi {name}, Kundennummer für Bestellung {ref}: {phone}\nBitte ruf noch einmal an und warte 5 Minuten an der Tür. Wenn immer noch niemand antwortet, schreib hier — fahr nicht mit dem Essen weg."},
    "cust_wait": {
        "en": "Hi {name}, got it — order {ref}. Our team is getting the customer's number for you right now.\nPlease stay at the address and ring once more. Don't leave with the food, we'll reply here in a moment.",
        "de": "Hi {name}, verstanden — Bestellung {ref}. Unser Team holt dir gerade die Nummer vom Kunden.\nBitte bleib an der Adresse und klingel noch einmal. Fahr nicht mit dem Essen weg, wir antworten hier gleich."},
    "address_wait": {
        "en": "Hi {name}, our team is checking the address for order {ref} now.{addr_line}\nPlease stay where you are and send your live location here if you can.",
        "de": "Hi {name}, unser Team prüft gerade die Adresse für Bestellung {ref}.{addr_line}\nBitte bleib wo du bist und schick uns hier deinen Live-Standort, wenn möglich."},
    "restaurant_wait": {
        "en": "Hi {name}, you've been at {restaurant} for {min} min (order {ref}). Please show the order number {ref} to the staff and ask how long it will take. If it's not ready in 5 minutes, write here and we'll call the restaurant.",
        "de": "Hi {name}, du bist seit {min} Min. bei {restaurant} (Bestellung {ref}). Zeig dem Personal bitte die Bestellnummer {ref} und frag, wie lange es noch dauert. Wenn es in 5 Minuten nicht fertig ist, schreib hier und wir rufen das Restaurant an."},
    "restaurant_long": {
        "en": "Hi {name}, {min} min is too long — our team is contacting the restaurant now about order {ref}. Please stay there, we'll update you here.",
        "de": "Hi {name}, {min} Min. sind zu lange — unser Team kontaktiert jetzt das Restaurant wegen Bestellung {ref}. Bitte bleib dort, wir melden uns hier."},
    "restaurant_no_order": {
        "en": "Hi {name}, our team is checking order {ref} with the restaurant now. Please stay at the restaurant and wait for our reply here.",
        "de": "Hi {name}, unser Team klärt Bestellung {ref} gerade mit dem Restaurant. Bitte bleib im Restaurant und warte auf unsere Antwort hier."},
    "problem_food": {
        "en": "Hi {name}, thanks for telling us. Please send a photo of the food/bag here. Our team will tell you what to do — please don't leave yet.",
        "de": "Hi {name}, danke für die Info. Bitte schick hier ein Foto vom Essen/von der Tüte. Unser Team sagt dir gleich, was zu tun ist — bitte fahr noch nicht weg."},
    "cancel": {
        "en": "Hi {name}, our team is checking order {ref} and will tell you what to do with the food. Please wait for our reply here.",
        "de": "Hi {name}, unser Team prüft Bestellung {ref} und sagt dir, was mit dem Essen passiert. Bitte warte hier auf unsere Antwort."},
    "handback": {
        "en": "Okay {name}. Our team is taking order {ref} off you and giving it to another rider now.\nPlease wait here for the confirmation before you leave or accept a new order.",
        "de": "Okay {name}. Unser Team nimmt dir Bestellung {ref} ab und gibt sie jetzt einem anderen Fahrer.\nBitte warte hier auf die Bestätigung, bevor du wegfährst oder eine neue Bestellung annimmst."},
    "handback_picked": {
        "en": "Hi {name}, you've already picked up order {ref}, so it can't go to another rider. Please deliver it — our team has your message and will reply here if needed.",
        "de": "Hi {name}, du hast Bestellung {ref} schon abgeholt, deshalb kann sie nicht an einen anderen Fahrer gehen. Bitte liefere sie aus — unser Team hat deine Nachricht und meldet sich hier, falls nötig."},
    "handback_done": {
        "en": "Done ✅ Order {ref} has been taken off you. You're free for the next order.",
        "de": "Erledigt ✅ Bestellung {ref} ist nicht mehr bei dir. Du bist frei für die nächste Bestellung."},
    "emergency": {
        "en": "Hi {name}, are you OK? If anyone is hurt, call 112 first. Our team has been alerted and will contact you right away.",
        "de": "Hi {name}, alles okay bei dir? Wenn jemand verletzt ist, ruf zuerst die 112 an. Unser Team ist informiert und meldet sich sofort bei dir."},
    "app_problem": {
        "en": "Hi {name}, please try:\n1. Close the app completely and open it again\n2. Check mobile data and GPS are on (location access: \"Always\")\n3. Log out and log in again\nStill stuck? Write here which step doesn't work — our team can complete it for you.",
        "de": "Hi {name}, bitte probier:\n1. App komplett schließen und neu öffnen\n2. Mobile Daten und GPS an (Standortzugriff: \"Immer\")\n3. Ab- und wieder anmelden\nGeht's immer noch nicht? Schreib hier, welcher Schritt nicht klappt — unser Team kann ihn für dich abschließen."},
    "status": {
        "en": "Hi {name}, your orders right now:\n{orders}",
        "de": "Hi {name}, deine Bestellungen gerade:\n{orders}"},
    "status_none": {
        "en": "Hi {name}, we don't see an active order on your account right now. If you just accepted one, give it a minute — or send the order number here.",
        "de": "Hi {name}, wir sehen gerade keine aktive Bestellung bei dir. Wenn du gerade eine angenommen hast, warte kurz — oder schick uns die Bestellnummer."},
    "shift_pay": {
        "en": "Hi {name}, thanks — this goes to the ops team. They'll answer you here as soon as possible.",
        "de": "Hi {name}, danke — das geht an das Ops-Team. Sie antworten dir hier so schnell wie möglich."},
    "ask_order": {
        "en": "Hi {name}, please send the order number from the app so we can help you faster.",
        "de": "Hi {name}, schick uns bitte die Bestellnummer aus der App, dann können wir dir schneller helfen."},
    "greeting": {
        "en": "Hi {name}! What's the problem? Please send the order number and a short description.",
        "de": "Hi {name}! Was ist los? Schick uns bitte die Bestellnummer und eine kurze Beschreibung."},
    "ack_unknown": {
        "en": "Thanks {name}, the team will reply here shortly.",
        "de": "Danke {name}, das Team antwortet dir hier gleich."},
}
TEMPLATE_LABEL = {
    "cust_phone": "Customer not reachable — number available", "cust_wait": "Customer not reachable — team fetches number",
    "address_wait": "Address problem", "restaurant_wait": "Waiting at restaurant (short)",
    "restaurant_long": "Waiting at restaurant (too long)", "restaurant_no_order": "Restaurant has no order / closed",
    "problem_food": "Food damaged / missing", "cancel": "Cancel / what to do with food",
    "handback": "Rider wants to hand back order", "handback_picked": "Hand back — already picked up",
    "handback_done": "Hand back confirmed (sent automatically when MotionTools shows it)", "emergency": "Accident / emergency",
    "app_problem": "App problem — first steps", "status": "Order status", "status_none": "Order status — none found",
    "shift_pay": "Shift / pay / admin", "ask_order": "Ask for order number", "greeting": "Greeting reply",
    "ack_unknown": "Unrecognised message (only if enabled)",
}

# Which intents need us to know the order
NEEDS_ORDER = {"customer_unreachable", "address_problem", "restaurant_wait", "restaurant_no_order", "problem_food", "cancel", "handback"}
# phase preference per intent (first match wins)
PHASE_PREF = {
    "customer_unreachable": ["at_customer", "to_customer"],
    "address_problem": ["to_customer", "at_customer"],
    "restaurant_wait": ["at_restaurant", "to_restaurant", "accepted"],
    "restaurant_no_order": ["at_restaurant", "to_restaurant", "accepted"],
    "problem_food": ["to_customer", "at_customer", "at_restaurant"],
    "cancel": ["at_customer", "to_customer", "at_restaurant"],
    "handback": ["at_restaurant", "to_restaurant", "accepted", "to_customer", "at_customer"],
}

DEFAULT_SETTINGS = {
    "bot_on": True,                 # master switch for visible replies (notes/escalation keep working)
    "reply_unknown": False,         # send "thanks, team will reply" to messages the bot doesn't understand
    "human_quiet_min": 20,          # a teammate wrote in the chat within this many minutes -> bot stays quiet
    "same_intent_min": 10,          # don't send the same answer twice within this window; escalate instead
    "max_replies_per_conv": 6,      # per 3 hours
    "restaurant_long_min": 12,      # waiting at the restaurant this long -> team calls the restaurant
    "escalate_assignee": "",        # Intercom team or admin id that gets escalated chats ("" = leave as is)
    "urgent_assignee": "",          # for emergencies ("" = same as escalate_assignee)
    "escalate_tag": "",             # Intercom tag id added to escalated chats (optional)
    "post_note": True,              # always post the internal context note
    "mt_order_url": "",             # e.g. https://quickzii.motiontools.io/bookings/{id}  ({id} = MotionTools booking id)
}


def fmt_t(v) -> str:
    d = ts(v)
    return d.astimezone(BERLIN).strftime("%H:%M") if d else "—"


def mins_since(v, now) -> int:
    d = ts(v)
    return max(0, int((now - d).total_seconds() // 60)) if d else 0


def pick_order(intent: str, live: list, named=None):
    if named:
        return named
    if not live:
        return None
    for ph in PHASE_PREF.get(intent, []):
        for o in live:
            if phase_of(o) == ph:
                return o
    return live[0]


def order_line(o: dict, lang: str, restaurant: str, now) -> str:
    ph = phase_of(o)
    label = PHASE_TEXT.get(ph, (ph, ph))[1 if lang == "de" else 0]
    parts = [f"• {o.get('ref')}: {label}"]
    if restaurant and ph in ("accepted", "to_restaurant", "at_restaurant"):
        parts.append(f"({restaurant})")
    if ph in ("accepted", "to_restaurant") and o.get("eta_restaurant"):
        parts.append(("— Restaurant ca. " if lang == "de" else "— restaurant ETA ") + fmt_t(o["eta_restaurant"]))
    if ph in ("at_restaurant", "to_customer", "at_customer") and (o.get("promised_at") or o.get("eta_customer")):
        parts.append(("— beim Kunden bis " if lang == "de" else "— at customer by ") + fmt_t(o.get("promised_at") or o.get("eta_customer")))
    return " ".join(parts)


@dataclass
class Decision:
    intent: str
    lang: str
    template: str = ""
    reply: str = ""
    escalate: bool = False
    urgent: bool = False
    reason: str = ""
    note: str = ""
    order_ref: str = ""
    order_id: str = ""
    scores: dict = field(default_factory=dict)


def render(templates: dict, key: str, lang: str, **kw) -> str:
    t = (templates.get(key) or DEFAULT_TEMPLATES[key]).get(lang) or DEFAULT_TEMPLATES[key][lang]

    class Safe(dict):
        def __missing__(self, k):
            return ""
    return t.format_map(Safe(**kw)).strip()


def decide(text: str, *, rider: dict | None, how: str, live: list, recent: list, named_order: dict | None,
           customer_phone: str, customer_addr: str, restaurant_name, city_name, settings: dict, templates: dict,
           history: list, human_recent: bool, now: datetime | None = None, detail_state: str = "",
           contact_name: str = "") -> Decision:
    """history = this conversation's earlier handled rows (newest first)."""
    now = now or datetime.now(UTC)
    intent, lang, scores = detect(text)
    d = Decision(intent=intent, lang=lang, scores=scores)
    first = ((rider or {}).get("name") or "").split(" ")[0] or (contact_name or "").split(" ")[0]
    if first and (any(ch.isdigit() for ch in first) or "@" in first):
        first = ""
    o = pick_order(intent, live, named_order) if (rider or named_order) else None
    if not o and named_order:
        o = named_order
    if not o and intent in NEEDS_ORDER and recent:
        o = recent[0]                                   # just delivered/cancelled — still the likely subject
    ref = (o or {}).get("ref") or ("deine Bestellung" if lang == "de" else "your order")
    d.order_ref = (o or {}).get("ref") or ""
    d.order_id = (o or {}).get("id") or ""
    rest = restaurant_name((o or {}).get("place_id")) or (o or {}).get("restaurant") or ("dem Restaurant" if lang == "de" else "the restaurant")
    kw = dict(name=first, ref=ref, restaurant=rest, phone=customer_phone)

    window = settings.get("same_intent_min", 10)
    same_recent = [h for h in history if h.get("intent") == intent and h.get("action", "").startswith("auto")
                   and mins_since(h.get("at"), now) < window]
    bot_replies = [h for h in history if h.get("action", "").startswith("auto") and mins_since(h.get("at"), now) < 180]

    # ---------- what to answer ----------
    tpl, esc, urgent, reason = "", False, False, ""
    if intent == "customer_unreachable":
        if not o and not rider:
            tpl, esc, reason = "ask_order", True, "rider not identified — team please check"
        elif customer_phone:
            tpl = "cust_phone"
        else:
            tpl, esc, reason = "cust_wait", True, "customer number needed (not available from MotionTools)"
    elif intent == "address_problem":
        tpl, esc, reason = "address_wait", True, "check the delivery address"
        kw["addr_line"] = (f"\nAddress in our system: {customer_addr}" if lang == "en" else f"\nAdresse bei uns: {customer_addr}") if customer_addr else ""
    elif intent == "restaurant_wait":
        waited = mins_since((o or {}).get("at_restaurant_at"), now) if (o or {}).get("at_restaurant_at") else 0
        kw["min"] = waited
        if o and phase_of(o) == "at_restaurant" and waited < settings.get("restaurant_long_min", 12) and not same_recent:
            tpl = "restaurant_wait"
        elif o and phase_of(o) == "at_restaurant":
            tpl, esc, reason = "restaurant_long", True, f"rider waiting {waited} min at the restaurant — call the restaurant"
        else:
            tpl, esc, reason = ("restaurant_no_order" if o else "ask_order"), True, "rider says food not ready (not marked 'at restaurant' in MotionTools)"
    elif intent == "restaurant_no_order":
        tpl, esc, reason = "restaurant_no_order", True, "restaurant has no order / closed — call the restaurant"
    elif intent == "problem_food":
        tpl, esc, reason = "problem_food", True, "food damaged / missing / wrong — decide what the rider does"
    elif intent == "cancel":
        tpl, esc, reason = "cancel", True, "cancel / what to do with the food"
    elif intent == "handback":
        if o and o.get("picked_up_at"):
            tpl, esc, reason = "handback_picked", True, f"rider wants to hand back order {ref}, but it is ALREADY PICKED UP"
        else:
            waited = mins_since((o or {}).get("at_restaurant_at"), now) if (o or {}).get("at_restaurant_at") else 0
            tpl, esc = "handback", True
            reason = (f"REDISPATCH order {ref} in MotionTools — rider wants to hand it back"
                      + (f" (waited {waited} min at the restaurant)" if waited else "")
                      + ". The bot confirms to the rider automatically once MotionTools shows it off him")
    elif intent == "emergency":
        tpl, esc, urgent, reason = "emergency", True, True, "ACCIDENT / BIKE / EMERGENCY — contact the rider now"
    elif intent == "app_problem":
        if same_recent or any(h.get("intent") == "app_problem" for h in history[:5]):
            tpl, esc, reason = "", True, "app problem again after first-steps reply — help the rider complete the step"
        else:
            tpl = "app_problem"
    elif intent == "order_status":
        if live:
            kw["orders"] = "\n".join(order_line(x, lang, restaurant_name(x.get("place_id")), now) for x in live)
            tpl = "status"
        elif rider:
            tpl = "status_none"
        else:
            tpl, esc, reason = "ask_order", True, "rider not identified"
    elif intent == "shift_pay":
        tpl, esc, reason = "shift_pay", True, "shift / pay / admin question"
    elif intent == "thanks":
        tpl = ""
    elif intent == "greeting":
        tpl = "" if any(h.get("intent") == "greeting" for h in history[:3]) else "greeting"
    else:
        esc, reason = True, "bot didn't understand — please answer"
        tpl = "ack_unknown" if settings.get("reply_unknown") else ""

    # needs the order but we don't know the rider and no code was sent -> ask for the order number too
    if intent in NEEDS_ORDER and not o and tpl not in ("ask_order", "emergency"):
        tpl, esc = "ask_order", True
        reason = reason or "rider not identified"

    # ---------- guards: human in the chat, repeats, flood ----------
    silent_why = ""
    if not settings.get("bot_on", True):
        silent_why = "bot switched off"
    elif human_recent and not urgent:
        silent_why = "teammate is handling this chat"
    elif same_recent and tpl and intent != "emergency":
        silent_why = "same answer sent a few minutes ago"
        esc = True
        reason = reason or "rider asked again after the bot answered"
    elif len(bot_replies) >= settings.get("max_replies_per_conv", 6):
        silent_why = "bot reply limit for this chat reached"
        esc = True
    if silent_why:
        d.reason = silent_why
        tpl = ""
    d.template = tpl
    d.reply = render(templates, tpl, lang, **kw) if tpl else ""
    d.reply = re.sub(r"^(Hi|Danke|Thanks) ([,!])", r"\1\2", d.reply)          # no name known -> "Hi, …"
    d.escalate, d.urgent = esc, urgent
    d.reason = "; ".join(x for x in [reason, silent_why] if x)

    # ---------- internal note for the team ----------
    lines = [("🚨 URGENT — " if urgent else "🤖 ") + "Rider helpdesk: " + INTENT_LABEL.get(intent, intent)]
    if rider:
        lines.append(f"Rider: {rider.get('name') or rider.get('id')}" + (f" ({city_name(rider.get('area'))})" if city_name(rider.get('area')) else "")
                     + (f" · {rider.get('phone')}" if rider.get("phone") else "") + f" · matched by {how}")
    else:
        lines.append("Rider: not matched to a MotionTools rider yet (link the contact in the helpdesk → Riders)")
    if o:
        ph = phase_of(o)
        bits = [f"Order {o.get('ref')}", PHASE_TEXT.get(ph, (ph,))[0]]
        if o.get("dispatched_at") and ph not in ("delivered", "cancelled"):
            bits.append(f"PTOD {mins_since(o['dispatched_at'], now)} min")
        if ph == "at_restaurant" and o.get("at_restaurant_at"):
            bits.append(f"at restaurant {mins_since(o['at_restaurant_at'], now)} min")
        if ph == "at_customer" and o.get("at_customer_at"):
            bits.append(f"at customer {mins_since(o['at_customer_at'], now)} min")
        r = restaurant_name(o.get("place_id"))
        if r:
            bits.append(f"restaurant {r}")
        if o.get("promised_at"):
            bits.append(f"planned {fmt_t(o['promised_at'])}")
        if o.get("eta_customer") and ph not in ("delivered", "cancelled"):
            bits.append(f"ETA {fmt_t(o['eta_customer'])}")
        if city_name(o.get("area")):
            bits.append(city_name(o.get("area")))
        lines.append(" · ".join(bits))
        url = settings.get("mt_order_url") or ""
        if url:
            lines.append(url.replace("{id}", o["id"]).replace("{ref}", str(o.get("ref") or "")))
    elif rider:
        lines.append("No live order on this rider in MotionTools right now.")
    others = [x for x in live if not o or x["id"] != o["id"]]
    if others:
        lines.append("Other live orders: " + ", ".join(f"{x.get('ref')} ({PHASE_TEXT.get(phase_of(x), ('',))[0]})" for x in others))
    if intent == "customer_unreachable":
        if customer_phone:
            lines.append(f"Customer phone (MotionTools): {customer_phone}")
        else:
            lines.append("Customer phone: not available through the API" + (f" ({detail_state})" if detail_state else "")
                         + " → look it up in MotionTools and send it to the rider.")
    if customer_addr and intent in ("address_problem", "customer_unreachable"):
        lines.append(f"Customer address: {customer_addr}")
    if d.reply:
        lines.append(f"Bot replied ({tpl}).")
    elif tpl == "" and d.reason:
        lines.append(f"Bot did not reply: {d.reason}.")
    if esc:
        lines.append("👉 Needs a teammate: " + (reason or "please take over"))
    d.note = "\n".join(lines)
    return d
