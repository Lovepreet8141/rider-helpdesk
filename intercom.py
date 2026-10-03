"""Minimal Intercom REST client (API version 2.11) — free with any Intercom workspace via a private Developer Hub app.

Without INTERCOM_TOKEN the client runs in *dry run*: nothing is sent, every call is recorded in `outbox`
so the dashboard (and the local test) shows exactly what would have been sent.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import re
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import httpx

log = logging.getLogger("intercom")
HOSTS = {"us": "https://api.intercom.io", "eu": "https://api.eu.intercom.io", "au": "https://api.au.intercom.io"}


def strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>|</p>|</div>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def to_html(text: str) -> str:
    return "".join(f"<p>{html.escape(line) if line.strip() else '<br>'}</p>" for line in text.split("\n"))


def signature_ok(secret: str, body: bytes, header: str) -> bool:
    """Intercom signs webhooks with X-Hub-Signature: sha1=<hmac-sha1(client_secret, raw body)>."""
    if not secret:
        return True
    want = "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(want, (header or "").strip())


class Intercom:
    def __init__(self, token: str, region: str = "us", admin_id: str = ""):
        self.token = token
        self.base = HOSTS.get((region or "us").lower(), HOSTS["us"])
        self.admin_id = admin_id
        self.outbox: deque = deque(maxlen=200)
        self.stats = {"calls": 0, "errors": 0, "last_error": None, "me": None}
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def dry(self) -> bool:
        return not self.token

    async def _req(self, method: str, path: str, json: dict = None):
        if self.dry:
            self.outbox.appendleft({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                    "method": method, "path": path, "json": json})
            return {"id": f"dry-{len(self.outbox)}", "type": "dry_run"}
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base, timeout=15, headers={
                "Authorization": f"Bearer {self.token}", "Accept": "application/json",
                "Content-Type": "application/json", "Intercom-Version": "2.11"})
        self.stats["calls"] += 1
        try:
            r = await self._client.request(method, path, json=json)
        except Exception as e:
            self.stats["errors"] += 1
            self.stats["last_error"] = f"{path}: {e}"
            log.warning("Intercom %s %s failed: %s", method, path, e)
            return None
        if r.status_code >= 300:
            self.stats["errors"] += 1
            self.stats["last_error"] = f"{method} {path} -> {r.status_code} {r.text[:300]}"
            log.warning("Intercom %s %s -> %s %s", method, path, r.status_code, r.text[:300])
            return None
        try:
            return r.json()
        except ValueError:
            return {}

    async def me(self):
        data = await self._req("GET", "/me")
        if data and not self.dry:
            self.stats["me"] = {"id": data.get("id"), "name": data.get("name"), "email": data.get("email"),
                                "app": (data.get("app") or {}).get("name")}
            if not self.admin_id and data.get("id"):
                self.admin_id = str(data["id"])
        return data

    async def conversation(self, cid: str):
        if self.dry:
            return None
        return await self._req("GET", f"/conversations/{cid}?display_as=plaintext")

    async def contact(self, contact_id: str):
        if self.dry:
            return None
        return await self._req("GET", f"/contacts/{contact_id}")

    async def reply(self, cid: str, text: str):
        """Visible reply to the rider (as the configured admin)."""
        return await self._req("POST", f"/conversations/{cid}/reply",
                               {"message_type": "comment", "type": "admin", "admin_id": self.admin_id, "body": to_html(text)})

    async def note(self, cid: str, text: str):
        """Internal note — only the Quickzi team sees it."""
        return await self._req("POST", f"/conversations/{cid}/reply",
                               {"message_type": "note", "type": "admin", "admin_id": self.admin_id, "body": to_html(text)})

    async def assign(self, cid: str, assignee_id: str):
        if not assignee_id:
            return None
        return await self._req("POST", f"/conversations/{cid}/parts",
                               {"message_type": "assignment", "type": "admin", "admin_id": self.admin_id,
                                "assignee_id": str(assignee_id)})

    async def tag(self, cid: str, tag_id: str):
        if not tag_id:
            return None
        return await self._req("POST", f"/conversations/{cid}/tags", {"id": str(tag_id), "admin_id": self.admin_id})
