# tce_monitor — мониторинг билетов на tce.by

Скрипт открывает страницы спектаклей через настоящий Chromium, дожидается
прохождения Anubis-челленджа, считает свободные места и шлёт уведомление
в Telegram, когда они появляются.

## Установка

1. Python 3.10+, далее:
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

2. Telegram-бот:
   - В Telegram открой **@BotFather**, команда `/newbot` → получишь токен вида `123456:ABC...`
   - Открой **@userinfobot**, отправь любое сообщение → получишь свой `chat_id`

3. Прописать токены как переменные окружения:
   ```bash
   export TELEGRAM_BOT_TOKEN="123456:ABC..."
   export TELEGRAM_CHAT_ID="123456789"
   ```

## Первый запуск — разведка

Нужно один раз посмотреть, как выглядит страница спектакля
после прохождения Anubis, и подобрать правильные селекторы свободных мест:

```bash
MODE=discovery python tce_monitor.py
```

После запуска появится папка `debug/` с `show_page.html` и `show_page.png`.
Открой HTML в браузере, найди элементы свободных мест (через DevTools),
и подставь точные селекторы в функцию `count_free_seats()` в `tce_monitor.py`.

## Настройка списка спектаклей

В `tce_monitor.py` отредактируй `SHOWS_TO_WATCH`:

```python
SHOWS_TO_WATCH = [
    {
        "name": "Колобок",
        "url": "https://tce.by/shows.html?base=...&data=...",
    },
    {
        "name": "Теремок",
        "url": "https://tce.by/shows.html?base=...&data=...",
    },
]
```

URL копируется со страницы конкретного спектакля.

## Запуск по расписанию

### Linux / macOS (cron)

```bash
crontab -e
```

Добавить строку (раз в 30 минут):

```cron
*/30 * * * * cd /path/to/tce_monitor && TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=yyy /usr/bin/python3 tce_monitor.py >> log.txt 2>&1
```

### Windows (Task Scheduler)

Создать задачу:
- Триггер: повторение каждые 30 минут
- Действие: `python C:\path\to\tce_monitor.py`
- Переменные окружения задать в свойствах задачи

## Замечания

- **Запускать раз в 20–30 минут, не чаще.** Сайт явно просит не парсить,
  и слишком частые запросы — повод их разозлить и получить полный бан по IP.
- **Один экземпляр.** Не запускать несколько копий параллельно.
- **Логи.** При ошибках смотри `debug/anubis_stuck.png` — там видно, на чём встал.
- **Если Anubis усложнится:** установи `playwright-stealth`
  (`pip install playwright-stealth`) и оберни context в стелс — это маскирует
  признаки автоматизации браузера. Сейчас не нужно, но имей в виду.
- **State-файл.** `tce_state.json` хранит последнее известное состояние.
  Если удалить — следующий запуск решит, что мест "раньше не было", и при
  любом наличии билетов сразу пришлёт уведомление.
