"""最小化的 Telegram Bot API 客户端：长轮询收消息、发/改消息、回应按钮。

只依赖 MoviePilot 自带的 httpx，不引入额外依赖。
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

import httpx


class TelegramError(Exception):
    pass


class TelegramConflict(TelegramError):
    """同一个 Token 正被别的程序 getUpdates，或设置了 Webhook。"""


class TelegramBot:
    def __init__(self, token: str, proxy: Optional[str] = None):
        self.base = f"https://api.telegram.org/bot{token}"
        self.client = httpx.Client(proxy=proxy or None, timeout=httpx.Timeout(10, read=40))

    def call(self, method: str, **params) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        r = self.client.post(f"{self.base}/{method}", json=params)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 409:
            raise TelegramConflict(data.get("description") or "409 Conflict")
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description') or r.status_code}")
        return data.get("result")

    def get_me(self) -> Dict:
        return self.call("getMe")

    def send(self, chat_id: int, text: str, reply_to: Optional[int] = None,
             buttons: Optional[List[List[Dict]]] = None) -> Dict:
        return self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                         link_preview_options={"is_disabled": True},
                         reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True}
                         if reply_to else None,
                         reply_markup={"inline_keyboard": buttons} if buttons else None)

    def edit(self, chat_id: int, message_id: int, text: str,
             buttons: Optional[List[List[Dict]]] = None) -> None:
        try:
            self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                      parse_mode="HTML", link_preview_options={"is_disabled": True},
                      reply_markup={"inline_keyboard": buttons or []})
        except TelegramError as e:
            # 内容没变时 Telegram 会报错，忽略即可
            if "not modified" not in str(e):
                raise

    def answer(self, callback_id: str, text: str = "", alert: bool = False) -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text or None,
                      show_alert=alert or None)
        except TelegramError:
            pass

    def poll(self, stop: threading.Event, on_update: Callable[[Dict], None],
             on_error: Callable[[Exception], None]) -> None:
        offset = None
        while not stop.is_set():
            try:
                updates = self.call("getUpdates", offset=offset, timeout=30,
                                    allowed_updates=["message", "callback_query"])
            except Exception as e:  # noqa: BLE001 - 网络错误不能让轮询线程退出
                on_error(e)
                stop.wait(30 if isinstance(e, TelegramConflict) else 5)
                continue
            for u in updates or []:
                offset = u["update_id"] + 1
                if stop.is_set():
                    break
                on_update(u)

    def close(self):
        self.client.close()


def button(text: str, data: str) -> Dict:
    return {"text": text, "callback_data": data}
