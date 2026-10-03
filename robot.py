"""MotionTools "robot teammate": signs in like a dashboard user instead of using the restricted API token.

The restricted API mode only blocks the integration token. A signed-in dashboard user reads the same
endpoints the web dashboard uses (verified: GET /api/hailing/bookings/{id} returns the drop-off contact).
Sign-in and token refresh are the documented ones (docs.motiontools.io):
  POST /api/signin         {"user": {"email", "password"}}  -> access_token, refresh_token, expires_in
  POST /api/refresh_token  {"refresh_token"}                -> access_token, expires_in

Use a dedicated MotionTools user for the robot (never a person's own login).
Env: MT_ROBOT_EMAIL, MT_ROBOT_PASSWORD
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from mt import BASE, MotionTools

log = logging.getLogger("robot")
UTC = timezone.utc
CLIENT = "Quickzi Rider Helpdesk/1.1"


class RobotMotionTools(MotionTools):
    def __init__(self, email: str, password: str, hourly_limit: int = 40):
        super().__init__(token="")
        self.email, self.password = email, password
        self.refresh_token = ""
        self.expires_at = None
        self.hourly_limit = hourly_limit
        self.calls_this_hour: list = []
        self.robot = {"signed_in": False, "last_signin": None, "error": None, "lookups": 0}
        self._lock = asyncio.Lock()
        self.stats["detail_path"] = "/api/hailing/bookings/{id}"     # the path the web dashboard itself uses

    @property
    def enabled(self) -> bool:
        return bool(self.email and self.password)

    def _headers(self):
        h = super()._headers()
        h["X-Client-Version"] = CLIENT
        h["Content-Type"] = "application/json"
        return h

    async def _auth(self, path: str, body: dict):
        async with httpx.AsyncClient(base_url=BASE, timeout=20) as c:
            r = await c.post(path, json=body, headers={"Accept": "application/json", "Content-Type": "application/json",
                                                       "X-Client-Version": CLIENT, "Accept-Language": "en"})
        if r.status_code >= 300:
            raise RuntimeError(f"{path} -> {r.status_code} {r.text[:200]}")
        d = r.json()
        d = d.get("data", d) if isinstance(d, dict) else {}
        self.token = d.get("access_token") or self.token
        self.refresh_token = d.get("refresh_token") or self.refresh_token
        self.expires_at = datetime.now(UTC) + timedelta(seconds=int(d.get("expires_in") or 3600) - 120)

    async def signin(self) -> bool:
        try:
            await self._auth("/api/signin", {"user": {"email": self.email, "password": self.password}})
            self.robot.update(signed_in=True, last_signin=datetime.now(UTC).isoformat(timespec="seconds"), error=None)
            for k in [k for k, why in self.blocked_paths.items() if why.startswith("http 40")]:
                self.blocked_paths.pop(k, None)
            return True
        except Exception as e:  # noqa: BLE001
            self.robot.update(signed_in=False, error=str(e)[:300])
            log.warning("robot sign-in failed: %s", e)
            return False

    async def ensure(self) -> bool:
        async with self._lock:
            if self.token and self.expires_at and datetime.now(UTC) < self.expires_at:
                return True
            if self.refresh_token:
                try:
                    await self._auth("/api/refresh_token", {"refresh_token": self.refresh_token})
                    return True
                except Exception as e:  # noqa: BLE001
                    log.info("robot refresh failed, signing in again: %s", e)
            return await self.signin()

    def _quota_ok(self) -> bool:
        cut = datetime.now(UTC) - timedelta(hours=1)
        self.calls_this_hour = [t for t in self.calls_this_hour if t > cut]
        return len(self.calls_this_hour) < self.hourly_limit

    async def _get(self, path: str, params: list, key: str = None):
        if not self._quota_ok():
            self.robot["error"] = f"hourly limit of {self.hourly_limit} lookups reached"
            return 429, None
        if not await self.ensure():
            return 401, None
        self.calls_this_hour.append(datetime.now(UTC))
        status, data = await super()._get(path, params, key)
        if status == 401:                              # token revoked/expired early -> sign in once more
            self.blocked_paths.pop(key or path, None)
            self.token, self.expires_at = "", None
            if await self.signin():
                status, data = await super()._get(path, params, key)
        if status == 200:
            self.robot["lookups"] += 1
        return status, data
