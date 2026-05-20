"""
tce_monitor.py — мониторинг билетов на tce.by через афиши театров

Архитектура:
1. Конфигурируем список театров (base ID) + опционально фильтр по названиям
2. Каждый запуск:
   - Заходит на афишу каждого театра, собирает все спектакли
   - Применяет фильтр (если задан)
   - Решает что проверять (с учётом "ленивого прорежения")
   - Для каждого нужного спектакля — открывает страницу, считает свободные места
   - Сравнивает с прошлым состоянием
   - Шлёт уведомления о появлении билетов

Запуск:
    pip install -r requirements.txt
    playwright install chromium
    python tce_monitor.py

Переменные окружения:
    TELEGRAM_BOT_TOKEN — токен бота от @BotFather
    TELEGRAM_CHAT_ID   — ID чата для уведомлений
    MODE               — "monitor" (по умолчанию) или "discovery"
    DISCOVERY_URL      — для discovery: какой URL разведать

Запускать через cron / GitHub Actions каждые 30 минут.
"""

import asyncio
import html
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout
import httpx


# ============ КОНФИГУРАЦИЯ ============

# Список театров для мониторинга.
# watch_titles: список подстрок имён. Пусто = следить за ВСЕМИ спектаклями театра.
# Поиск регистронезависимый, по совпадению подстроки.
THEATERS = [
    {
        "name": "Театр кукол",
        "base": "RkZDMTE2MUQtMTNFNy00NUIyLTg0QzYtMURDMjRBNTc1ODA0",
        "watch_titles": [],
    },
    {
        "name": "Театр юного зрителя",
        "base": "RUMxRjFDNzgtRkRDOS00NjI3LTg3QzAtMTlFOTk0MkNEQ0Yy",
        "watch_titles": [],
    },
]

# Пауза между запросами к разным страницам — имитация человеческого поведения
POLITENESS_DELAY_SECONDS = 1.0

# Стоп при серии ошибок
MAX_ERRORS_IN_ROW = 5

# Ежедневный "я живой" пинг в группу. Время в часах по Минску (UTC+3).
# Например 9 = в 9 утра по Минску. None = отключить.
DAILY_HEARTBEAT_HOUR_MINSK = 9

STATE_FILE = Path(__file__).parent / "tce_state.json"
DEBUG_DIR  = Path(__file__).parent / "debug"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")
MODE = os.environ.get("MODE", "monitor")
DISCOVERY_URL = os.environ.get("DISCOVERY_URL")


# ============ TELEGRAM ============

async def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TG не настроен]\n{message}\n")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            })
            r.raise_for_status()
        except Exception as e:
            print(f"[ERROR telegram] {e}", file=sys.stderr)


def format_show_message(show: dict) -> str:
    """Красивое уведомление об одном спектакле с появившимися билетами."""
    return (
        f"🎭 <b>{html.escape(show['name'])}</b>\n"
        f"📅 {html.escape(show['date'])}\n"
        f"📍 {html.escape(show['venue'])}\n\n"
        f"✅ Появились билеты: <b>{show['free']} мест</b>\n\n"
        f"<a href=\"{html.escape(show['url'])}\">👉 Открыть и купить</a>"
    )


def format_digest_message(shows: list) -> str:
    """Сводное сообщение для нескольких спектаклей сразу."""
    lines = [f"🎭 <b>Билеты появились сразу на {len(shows)} спектаклей:</b>\n"]
    for s in shows:
        lines.append(
            f"• <a href=\"{html.escape(s['url'])}\">"
            f"{html.escape(s['name'])}</a> — "
            f"{html.escape(s['date'])} ({s['free']} мест, {html.escape(s['venue'])})"
        )
    return "\n".join(lines)


# ============ HEARTBEAT ============

async def maybe_send_heartbeat(state: dict, all_shows: list, now: datetime):
    """Раз в сутки шлёт "я живой" пинг в группу.

    Хранит дату последнего пинга в state["__meta__"]["last_heartbeat_date"].
    Шлёт когда: уже наступил час DAILY_HEARTBEAT_HOUR_MINSK по Минску
    И сегодня ещё не слал.
    """
    if DAILY_HEARTBEAT_HOUR_MINSK is None:
        return

    # Минск = UTC+3 (без учёта переходов, в РБ их нет с 2014)
    minsk_now = now + timedelta(hours=3)
    today_minsk = minsk_now.date().isoformat()

    meta = state.setdefault("__meta__", {})
    last_date = meta.get("last_heartbeat_date")

    # Уже слали сегодня — пропустить
    if last_date == today_minsk:
        return

    # Час по Минску ещё не наступил — пропустить
    if minsk_now.hour < DAILY_HEARTBEAT_HOUR_MINSK:
        return

    # Статистика по театрам
    per_theater = {}
    for s in all_shows:
        t = s.get("theater_name", "?")
        per_theater[t] = per_theater.get(t, 0) + 1

    lines = [
        f"🤖 <b>Доброе утро!</b>",
        f"Бот работает, билетов пока нет.",
        f"",
        f"<b>Под наблюдением:</b>",
    ]
    for t, n in sorted(per_theater.items()):
        lines.append(f"• {html.escape(t)} — {n} спектаклей")

    total = sum(per_theater.values())
    lines.append(f"")
    lines.append(f"<i>Всего: {total} спектаклей. Проверяю каждые 30 минут.</i>")

    await send_telegram("\n".join(lines))
    meta["last_heartbeat_date"] = today_minsk
    save_state(state)  # пересохранить с обновлённой датой


# ============ STATE ============

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def update_state_entry(entry: dict, now: datetime, free: int, show: dict) -> dict:
    """Обновляет запись в state."""
    entry["name"] = show["name"]
    entry["date"] = show["date"]
    entry["venue"] = show["venue"]
    entry["theater"] = show.get("theater_name", "")
    entry["last_count"] = free
    entry["last_check"] = now.isoformat()
    return entry


# ============ РАБОТА СО СТРАНИЦЕЙ ============

async def open_page(page, url: str, attempt: int = 1):
    """Открывает URL и ждёт прохождения Anubis-челленджа. Делает ретрай на сетевых ошибках."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    except Exception as e:
        # Сетевые ошибки (моргание WiFi, разрыв соединения) — ретрай один раз
        net_errors = ("ERR_NETWORK_CHANGED", "ERR_INTERNET_DISCONNECTED",
                      "ERR_CONNECTION_RESET", "interrupted by another navigation",
                      "ERR_NAME_NOT_RESOLVED", "ERR_TIMED_OUT")
        if attempt == 1 and any(s in str(e) for s in net_errors):
            print(f"  [retry] сетевая ошибка ({type(e).__name__}), жду 5 сек и пробую снова")
            await asyncio.sleep(5)
            return await open_page(page, url, attempt=2)
        raise

    try:
        await page.wait_for_function(
            """() => {
                if (!document.body) return false;
                if (document.getElementById('anubis_challenge')) return false;
                const t = (document.body.innerText || '').toLowerCase();
                if (t.includes('not a bot')) return false;
                if (t.includes('не бот')) return false;
                if (t.includes('проверяем')) return false;
                return true;
            }""",
            timeout=60_000,
        )
    except PWTimeout:
        DEBUG_DIR.mkdir(exist_ok=True)
        await page.screenshot(path=str(DEBUG_DIR / "anubis_stuck.png"))
        raise RuntimeError("Anubis не пропустил за 60 сек")

    # На сайте tce.by есть индикатор загрузки <div id="loading"> с текстом
    # "Подождите, идет загрузка данных...". Он показывается пока AJAX-запрос
    # за схемой зала / списком спектаклей в работе, и прячется когда готов.
    # Это самый надёжный признак "страница полностью готова".
    try:
        await page.wait_for_function(
            """() => {
                const el = document.getElementById('loading');
                if (!el) return true;            // нет такого элемента — значит и не надо ждать
                if (el.offsetParent === null) return true;  // элемент скрыт (display:none)
                return false;                    // ещё видим — ждём
            }""",
            timeout=20_000,
        )
    except PWTimeout:
        pass  # если не дождались — пусть caller сам разбирается
    # Не ждём networkidle — на этих страницах он редко наступает,
    # уводит время на 20с таймауты. Caller сам подождёт нужный элемент.


async def fetch_afisha(page, theater: dict) -> list:
    """Парсит афишу театра, возвращает список спектаклей."""
    url = f"https://tce.by/index.html?base={theater['base']}"
    await open_page(page, url)

    # Таблица заполняется AJAX-запросом после нажатия кнопки "Найти".
    # На странице есть авто-клик, но он иногда не успевает / падает —
    # делаем явный клик и ждём появления строк.
    try:
        # Жмём "Найти" вручную — это идемпотентно (если строки уже есть, ничего страшного)
        try:
            await page.click("#reload", timeout=5_000)
        except Exception:
            pass  # кнопки нет / уже отработали — не критично

        # Ждём появления хотя бы одной строки в tbody
        await page.wait_for_function(
            "() => document.querySelectorAll('#playbill tbody tr').length > 0",
            timeout=20_000,
        )
    except PWTimeout:
        # Возможно афиша реально пустая или AJAX упал. Попробуем ещё раз.
        try:
            await page.click("#reload", timeout=5_000)
            await page.wait_for_function(
                "() => document.querySelectorAll('#playbill tbody tr').length > 0",
                timeout=15_000,
            )
        except Exception:
            print(f"  [WARN] афиша {theater['name']} пустая (или AJAX не ответил)")

    rows = await page.evaluate(r"""
        () => {
            const rows = document.querySelectorAll('#playbill tbody tr');
            const result = [];
            for (const r of rows) {
                const cells = r.querySelectorAll('td');
                if (cells.length < 3) continue;
                const linkEl = cells[1].querySelector('a');
                if (!linkEl) continue;
                result.push({
                    date:  (cells[0].innerText || '').trim(),
                    name:  (cells[1].innerText || '').trim(),
                    venue: (cells[2].innerText || '').trim(),
                    url:   linkEl.href,
                });
            }
            return result;
        }
    """)
    return [r for r in rows if "shows.html" in r["url"]]


def filter_shows(shows: list, watch_titles: list) -> list:
    if not watch_titles:
        return shows
    needles = [t.lower() for t in watch_titles]
    return [s for s in shows if any(n in s["name"].lower() for n in needles)]


async def count_free_seats(page) -> int:
    """Считает количество свободных мест на странице спектакля.

    Схема зала на странице спектакля грузится через AJAX (doRequest 'shows'
    'ticket'). До этого момента все места рендерятся одним классом 'place'
    без data-col. После AJAX:
      - занятые места получают data-col / data-row
      - свободные дополнительно получают класс 'zone' + конкретную ценовую
        зону (zone1080, zone1081 и т.п.)

    Ждём пока на странице окажется заметное количество мест с data-col
    (это значит AJAX точно отработал), плюс ещё короткая пауза на случай
    нескольких проходов.
    """
    # Сначала убедимся что есть хоть какие-то места (структура есть)
    try:
        await page.wait_for_selector("td.place", timeout=10_000)
    except PWTimeout:
        return 0

    # Ждём пока AJAX заполнит данные. Признак: появились места с data-col.
    # Используем большой таймаут — на GitHub Actions сеть может быть медленной.
    try:
        await page.wait_for_function(
            "() => document.querySelectorAll('td.place[data-col]').length > 0",
            timeout=30_000,
        )
    except PWTimeout:
        # AJAX так и не отработал. Это бывает если для конкретного спектакля
        # схемы зала нет вообще (например, концерт без рассадки).
        # Не возвращаем 0 сразу — попробуем посчитать что есть.
        pass

    # Небольшая пауза на случай если AJAX дозаполняет классы 'zone' порциями
    await asyncio.sleep(1.5)

    free = await page.evaluate(r"""
        () => document.querySelectorAll('td.zone').length
    """)
    return int(free)


# ============ РЕЖИМ РАЗВЕДКИ ============

async def run_discovery(page, url: str):
    DEBUG_DIR.mkdir(exist_ok=True)
    await open_page(page, url)

    # Если это страница спектакля — дождаться загрузки схемы зала
    # (иначе скриншот будет с надписью "Подождите...")
    if "shows.html" in url:
        try:
            await page.wait_for_function(
                "() => document.querySelectorAll('td.place[data-col]').length > 0",
                timeout=30_000,
            )
            await asyncio.sleep(1.5)
        except PWTimeout:
            print("[WARN] схема зала не догрузилась за 30 сек, сохраняю как есть")

    prefix = "afisha" if "index.html" in url else "show"
    html_path = DEBUG_DIR / f"{prefix}_page.html"
    png_path  = DEBUG_DIR / f"{prefix}_page.png"
    html_path.write_text(await page.content(), encoding="utf-8")
    await page.screenshot(path=str(png_path), full_page=True)
    print(f"[OK] HTML: {html_path}")
    print(f"[OK] PNG : {png_path}")


# ============ ОСНОВНОЙ ЦИКЛ ============

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="ru-RU",
        )
        page = await context.new_page()

        # --- режим разведки ---
        if MODE == "discovery":
            url = DISCOVERY_URL or f"https://tce.by/index.html?base={THEATERS[0]['base']}"
            await run_discovery(page, url)
            await browser.close()
            return

        # --- основной режим ---
        state = load_state()
        now = datetime.now(timezone.utc)
        notifications = []

        # ШАГ 1. Собираем афиши всех театров
        all_shows = []
        for th in THEATERS:
            try:
                shows = await fetch_afisha(page, th)
                shows = filter_shows(shows, th.get("watch_titles", []))
                for s in shows:
                    s["theater_name"] = th["name"]
                all_shows.extend(shows)
                print(f"[АФИША] {th['name']}: {len(shows)} спектаклей после фильтра")
                await asyncio.sleep(POLITENESS_DELAY_SECONDS)
            except Exception as e:
                print(f"[ERR афиша] {th['name']}: {e}", file=sys.stderr)

        # ШАГ 2. Проверяем ВСЕ спектакли каждый запуск. Нулевые места —
        # это как раз то что мы ищем (момент 0 → >0), пропускать их нельзя.
        to_check = all_shows
        print(f"\n[ПЛАН] Всего: {len(all_shows)}, проверяю все")

        # ШАГ 3. Проверяем
        errors_in_row = 0
        for show in to_check:
            try:
                await open_page(page, show["url"])
                free = await count_free_seats(page)

                prev_entry = state.get(show["url"], {})
                prev_count = prev_entry.get("last_count", -1)

                state[show["url"]] = update_state_entry(
                    dict(prev_entry), now, free, show
                )

                stamp = now.strftime("%H:%M")
                print(f"  [{stamp}] {show['theater_name']} | "
                      f"{show['name']} {show['date']}: {free} мест "
                      f"(прежде: {prev_count})")

                # Уведомляем: появились места после "пусто" или впервые увидели
                if free > 0 and prev_count <= 0:
                    notifications.append({**show, "free": free})

                errors_in_row = 0
                await asyncio.sleep(POLITENESS_DELAY_SECONDS)
            except Exception as e:
                errors_in_row += 1
                print(f"[ERR] {show['name']}: {e}", file=sys.stderr)
                if errors_in_row >= MAX_ERRORS_IN_ROW:
                    await send_telegram(
                        f"⚠️ <b>tce_monitor</b>: {MAX_ERRORS_IN_ROW} ошибок подряд, "
                        "останавливаю текущий прогон. Следующая попытка через 30 мин."
                    )
                    break

        # ШАГ 4. Чистка устаревших (которых больше нет в афише)
        active_urls = {s["url"] for s in all_shows}
        for url in list(state.keys()):
            if url.startswith("__"):  # служебные ключи типа __meta__ не трогаем
                continue
            if url not in active_urls:
                del state[url]

        save_state(state)
        await browser.close()

        # Ежедневный heartbeat — отдельно от уведомлений, всегда после save_state
        await maybe_send_heartbeat(state, all_shows, now)

        # ШАГ 5. Уведомления
        if not notifications:
            print("\n[ИТОГ] Изменений нет.")
        elif len(notifications) <= 5:
            print(f"\n[ИТОГ] Отправляю {len(notifications)} уведомлений.")
            for n in notifications:
                await send_telegram(format_show_message(n))
                await asyncio.sleep(0.5)
        else:
            print(f"\n[ИТОГ] Слишком много ({len(notifications)}), шлю сводку.")
            await send_telegram(format_digest_message(notifications))


if __name__ == "__main__":
    asyncio.run(main())
