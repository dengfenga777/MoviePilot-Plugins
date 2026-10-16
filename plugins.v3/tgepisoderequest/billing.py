"""PMS 积分计费：调用 PMSManageBot 的内部接口查余额、扣积分，并维护每月免费额度。"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional

import httpx


class PmsError(Exception):
    pass


class PmsUserNotFound(PmsError):
    pass


class PmsInsufficient(PmsError):
    def __init__(self, message: str, balance: Optional[float] = None):
        super().__init__(message)
        self.balance = balance


class PmsClient:
    def __init__(self, base_url: str, token: str, timeout: float = 10):
        self.base = base_url.rstrip("/") + "/api/internal/credits"
        self.headers = {"X-Api-Key": token}
        self.timeout = timeout

    def _call(self, method: str, url: str, **kw) -> Dict:
        try:
            r = httpx.request(method, url, headers=self.headers, timeout=self.timeout, **kw)
        except httpx.HTTPError as e:
            raise PmsError(f"连接 PMS 失败：{e}") from e
        try:
            data = r.json()
        except ValueError:
            data = {}
        detail = data.get("detail") if isinstance(data, dict) else None
        if r.status_code == 404:
            raise PmsUserNotFound(str(detail or "用户不存在"))
        if r.status_code == 402:
            detail = detail if isinstance(detail, dict) else {}
            raise PmsInsufficient(detail.get("message") or "积分不足", detail.get("credits"))
        if r.status_code != 200 or not data.get("success"):
            raise PmsError(f"PMS 返回 {r.status_code}：{detail or r.text[:200]}")
        return data

    def balance(self, tg_id: int) -> float:
        return float(self._call("GET", self.base, params={"tg_id": tg_id})["credits"])

    def charge(self, tg_id: int, amount: float, ref: str, reason: str = "") -> Dict:
        return self._call("POST", f"{self.base}/charge",
                          json={"tg_id": tg_id, "amount": amount, "ref": ref, "reason": reason[:200]})


def month_key(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m")


def charge_key(tmdbid: int, is_movie: bool, season: Optional[int]) -> str:
    """同一个人求同一部电影 / 同一季只收一次。"""
    return f"movie:{tmdbid}" if is_movie else f"tv:{tmdbid}:s{season or 0}"


class Usage:
    """每月免费额度用量 + 已付费（或已用额度）的条目。数据是普通 dict，方便 save_data。"""

    def __init__(self, data: Optional[Dict] = None, now: Optional[datetime] = None):
        data = data or {}
        month = month_key(now)
        self.month = month
        self.used: Dict[str, Dict[str, int]] = data.get("used", {}) if data.get("month") == month else {}
        self.paid: Dict[str, list] = data.get("paid", {})

    def to_dict(self) -> Dict:
        return {"month": self.month, "used": self.used, "paid": self.paid}

    def is_paid(self, tg_id: int, key: str) -> bool:
        return key in self.paid.get(str(tg_id), [])

    def mark_paid(self, tg_id: int, key: str, limit: int = 500):
        items = self.paid.setdefault(str(tg_id), [])
        if key not in items:
            items.append(key)
            del items[:-limit]

    def left(self, tg_id: int, kind: str, quota: int) -> int:
        return max(0, quota - self.used.get(str(tg_id), {}).get(kind, 0))

    def consume(self, tg_id: int, kind: str):
        u = self.used.setdefault(str(tg_id), {})
        u[kind] = u.get(kind, 0) + 1
