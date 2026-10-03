# Quickzi Rider Helpdesk — Intercom ↔ MotionTools (v1.2)

Riders write to Quickzi in Intercom. This service reads every rider message, matches the rider and his live order from
MotionTools, and
- **answers simple questions in the chat straight away** (in German or English, depending on what the rider writes)
- **hands harder cases to the team**: it tells the rider "team is on it", posts an **internal note** with the full order
  context, and assigns the chat to your team.

It is separate from Quickzi Ops: it has its own repo, its own Railway service and its own database, and covers **all cities**
(Munich, Hamburg, Hamburg-Harburg, Aachen).

## What the bot handles

| Rider writes (EN/DE, a few Hindi/Arabic phrases) | Bot replies | Team gets it? |
|---|---|---|
| "Customer not answering / Kunde geht nicht ran / send customer number" | "Stay at the door, ring again, team is getting the number" | **Yes**: note with order, minutes at customer, restaurant, city |
| Same, and MotionTools gives the customer phone | Sends the number + "call again, wait 5 min" | No |
| "Food not ready / Essen nicht fertig" (< 12 min at restaurant) | "You've been at *X* for *N* min, show order *ref* to staff…" | No |
| Same, ≥ 12 min, or asked twice | "Too long, team is contacting the restaurant" | **Yes** |
| "Restaurant has no order / closed" | "Team is checking with the restaurant, stay there" | **Yes** |
| "Can't find the address / Hausnummer" | "Team checking address, send live location" | **Yes** |
| "Which order do I have? / status" | Lists his live orders with stage and deadline | No |
| "App not working / can't swipe" | 3 first steps (restart, GPS, re-login) | Only if he writes again |
| Food spilled / missing / wrong | "Send a photo, don't leave yet" | **Yes** |
| **"I don't want to wait / take this order off me / ich will nicht mehr warten"** | "Team is giving order *ref* to another rider, wait for confirmation" | **Yes**: note says **REDISPATCH order *ref*** (+ minutes waited) |
| …then your team redispatches in MotionTools | Bot tells the rider automatically: "Done ✅ order *ref* is off you, you're free" | No |
| Same, but he already picked the food up | "Already picked up, please deliver it" | **Yes** |
| Customer cancelled / what to do with food | "Team will tell you, wait here" | **Yes** |
| Accident / police / flat tyre / bike broken | "Are you OK? Call 112 if hurt" | **Yes, URGENT** |
| Shift / salary / sick / hours | "Goes to the ops team" | **Yes** |
| "Hi" / "Danke" | Greeting / nothing | No |
| Anything else | Nothing (optional "team will reply") | **Yes** |

> **Redispatching itself is done by your team in MotionTools.** Your restricted API token can't change orders, so the
> bot handles everything around it: it recognises the request, tells the team exactly which order to redispatch, and closes the loop
> with the rider as soon as MotionTools shows the order went back to the pool or to another rider.

**Safety rules built in**
- When a teammate has written in the chat in the last 20 min, the bot stays quiet. Accidents are the exception.
- The bot never sends the same answer twice within 10 min. A repeat question goes to the team instead.
- The bot sends at most 6 replies per chat in 3 hours.
- One switch turns off all bot replies. Notes and hand-overs keep working.

**How it finds the rider**
1. A saved link between his Intercom contact and his MotionTools rider.
2. An order number in his message.
3. His phone number.
4. His name.

A match is saved automatically. When the bot can't tell who the rider is, it asks for the order number and hands the
chat to the team. You can link a rider by hand in the dashboard.

> **Customer phone numbers:** your MotionTools account is in restricted API mode, so the customer's phone number can't be
> read automatically. The bot therefore tells the rider to wait and gives the team everything else in one note. If
> MotionTools ever opens `GET /api/bookings/{id}` for your token, set `MT_API_TOKEN`. The bot then sends the number
> itself, with no code change.

## MotionTools robot (v1.2) — customer numbers automatically
Your restricted mode only blocks the **API token**. A **signed-in dashboard user** can read the same order data the web
dashboard shows: `GET /api/hailing/bookings/{id}` was tested on 4 Oct with a live Hamburg order and returned the
drop-off contact. So the robot signs in as its own MotionTools user using the documented
`/api/signin` and `/api/refresh_token`, and looks up **only the order a rider is asking about**.
- Rider: "customer not answering" → bot replies within seconds with the customer's number + "call again, wait 5 min".
  Solved, no teammate needed. The note also shows the address.
- Max 40 lookups per hour (`MT_ROBOT_HOURLY_LIMIT`). One lookup per order, then it's cached.
- If sign-in fails, everything falls back to the team-fetches-number flow. You can see the status under System → MotionTools robot.

**Setup:** in MotionTools → Drivers/Users, create a separate operator user for the robot (e.g. `robot@…`, with the
same role your dispatchers have). Never use your own login. Add to Railway:
`MT_ROBOT_EMAIL`, `MT_ROBOT_PASSWORD` (optional `MT_ROBOT_HOURLY_LIMIT=40`).

**Redispatch is not automated yet.** The dashboard has an "unclaim" action on tours (takes the order off the rider),
but its exact request has not been checked. It will be added after one real redispatch in the dashboard has been
observed, so the robot only sends what the dashboard sends.

## Dashboard — `/help`
**Inbox** shows every rider message and what happened: solved by the bot, sent to the team, or urgent. The other tabs:
- **Try a message**: type anything a rider might write and see the exact reply and note. Nothing is sent.
- **Riders**: who is matched to which Intercom contact, plus manual linking.
- **Replies**: edit every reply text in EN and DE.
- **Settings**: bot on/off, time limits, who gets the hand-overs, city and restaurant names.
- **System**: whether the Intercom and MotionTools webhooks are arriving.

---

## Setup (about 20 min)

### 1. GitHub
Create a new repo, **`Lovepreet8141/rider-helpdesk`**. Upload all files from this folder, as you do for rider-alerts.

### 2. Railway — new service in the same project as Quickzi Ops
1. **New → GitHub Repo →** `rider-helpdesk`.
2. **Add a Volume** mounted at `/data`.
3. **Settings → Networking → Generate Domain**, e.g. `rider-helpdesk-production.up.railway.app`.
4. **Variables:**

| Variable | Value |
|---|---|
| `DASHBOARD_PASSWORD` | a password for `/help` |
| `WEBHOOK_PATH_SECRET` | a long random word, e.g. `qz-7f3k9-helpdesk` |
| `DATA_DIR` | `/data` |
| `INTERCOM_TOKEN` | from step 3 |
| `INTERCOM_CLIENT_SECRET` | from step 3 (checks that webhooks really come from Intercom) |
| `INTERCOM_REGION` | `eu` if your Intercom data is hosted in Europe, otherwise leave empty (`us`) |
| `INTERCOM_ADMIN_ID` | optional: the teammate the bot replies as (default: the owner of the token) |
| `MT_API_TOKEN` | optional: the same MotionTools token as Quickzi Ops |

Until `INTERCOM_TOKEN` is set, the service runs in **dry run**: it reads everything and shows what it *would* send
under System, but sends nothing.

### 3. Intercom — private app (free)
1. Go to Intercom → **Settings → Integrations → Developer Hub → New app**. Name it "Quickzi Rider Helpdesk" and pick your workspace.
2. **Authentication**: copy the **Access token** into `INTERCOM_TOKEN`. Under **Basic information**, copy the **Client secret** into `INTERCOM_CLIENT_SECRET`.
3. **Permissions** (Authentication → Edit): *Read conversations*, *Write conversations*, and *Read and list users and companies* (contacts). Add *Read tags / Write tags* if you want tags.
4. **Webhooks**: set the endpoint URL to `https://<your-railway-domain>/intercom/<WEBHOOK_PATH_SECRET>`.
   Topics: **`conversation.user.created`** and **`conversation.user.replied`**. Save, then press **Send test request**. System should show 1 event.
5. Optional, recommended: find the id of the **team** that should get hand-overs. Open the team inbox; the number in the
   browser URL is its id. Paste it under Settings → *Assign hand-overs to*.

### 4. MotionTools — one more webhook (all cities)
In `quickzii.motiontools.io`, go to **Settings → Webhooks → New** and name it "Rider Helpdesk":
- URL: `https://<your-railway-domain>/mt/<WEBHOOK_PATH_SECRET>`
- Events: `booking.created`, `booking.transition`, `booking.in_progress`, `booking.stop_arrived`,
  `booking.stop_completed`, `booking.stop_failed`, `booking.etas_recalculated`, `driver.online`, `driver.offline`,
  `tour.created`, `tour.transition`
- No service-area filter, so all cities come in. No GPS events are needed.

Leave your existing Quickzi Ops webhooks as they are.

### 5. Go live safely
1. Open `/help` → **Settings**, set **Bot answers riders = Off** and save. The bot now only posts notes and hand-overs.
2. Run one evening like this. Every morning, check **Inbox → Rider not recognised**, link any unmatched riders, and
   add the city names (area id → Munich/Hamburg/Aachen) and restaurant names.
3. Use **Try a message** to check the replies. Change the wording under **Replies** if needed.
4. Switch **Bot answers riders = On**.

## Cost
- The Intercom private app, its webhooks and the API are free on every Intercom plan. It does **not** use Fin, so there are no per-resolution charges.
- MotionTools webhooks are free.
- Railway: this is a second small service in your existing project, so it adds a little usage to the same plan. It
  needs no AI API and no paid service.

## Files
`app.py` server and webhooks · `brain.py` question detection and replies · `tracker.py` orders and riders from MotionTools events ·
`intercom.py` Intercom client · `mt.py` MotionTools client (same as Quickzi Ops) · `store.py` SQLite · `help.html` dashboard ·
`robot.py` MotionTools robot login · `test_local.py` full local test · `test_robot.py` robot test

## Test locally
```
pip install -r requirements.txt
python3 test_local.py           # 29 checks: matching, replies, hand-overs, repeats, urgent, signature, teammate takeover, hand-back + confirmation
python3 test_robot.py           # 7 checks: robot sign-in, re-sign-in, customer number to rider, caching
python3 test_local.py --serve   # then open http://127.0.0.1:8031/help (password: test)
```
