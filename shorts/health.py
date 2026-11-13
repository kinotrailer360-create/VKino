"""Здоровье источников новостей: раз в час — читаются ли Telegram-каналы, ленты и быстрые источники (Reddit, X,
Deadline, Empire…). Ошибка FAIL_ALERT раз подряд — одно предупреждение админу, починился — одно сообщение об этом.
«Новостей нет» без ошибки — не тревога (источник просто молчит), только пометка в /sources."""

from __future__ import annotations

import time
from typing import Any

FAIL_ALERT = 3  # проверок подряд с ошибкой (≈ 3 часа) — тогда предупреждаем
QUIET_HOURS = 72  # без новостей дольше — пометка «молчит» (не тревога)


def update(state: dict[str, dict[str, Any]], results: list[tuple[str, str, int | str]],
           now: float | None = None) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """results — (ключ, имя для автора, число новостей | текст ошибки). Возвращает новое состояние и сообщения
    админу (сломался / снова работает)."""
    now = now or time.time()
    out: list[str] = []
    for key, name, res in results:
        st = dict(state.get(key) or {"name": name, "streak": 0, "alerted": False})
        st["name"] = name
        st["checked"] = now
        if isinstance(res, int):
            st["ok"] = now
            if res > 0:
                st["item"] = now
            st["items"] = res
            if st.get("alerted"):
                out.append(f"✅ Снова читается: {name}")
            st["streak"], st["alerted"], st["error"] = 0, False, ""
        else:
            st["streak"] = int(st.get("streak") or 0) + 1
            st["error"] = str(res)[:120]
            if st["streak"] >= FAIL_ALERT and not st.get("alerted"):
                st["alerted"] = True
                since = st.get("ok")
                ago = f" (последний раз читался {int((now - since) // 3600)} ч назад)" if since else ""
                out.append(f"⚠️ Не читается {st['streak']} проверки подряд: {name}{ago} — {st['error']}")
        state[key] = st
    return state, out


def status_line(st: dict[str, Any], now: float | None = None) -> str:
    now = now or time.time()

    def ago(t: float | None) -> str:
        if not t:
            return "никогда"
        h = int((now - t) // 3600)
        return "только что" if h < 1 else f"{h} ч назад" if h < 48 else f"{h // 24} дн назад"

    if st.get("streak"):
        icon = "❌"
    elif st.get("item") and now - st["item"] > QUIET_HOURS * 3600:
        icon = "⚪"  # читается, но давно без новостей
    else:
        icon = "✅"
    tail = f" — ошибка {st['streak']}× подряд: {st.get('error', '')}" if st.get("streak") else ""
    return (f"{icon} {st.get('name', '')}: читался {ago(st.get('ok'))}, новость {ago(st.get('item'))}"
            f" (за сутки {st.get('items', 0)}){tail}")
