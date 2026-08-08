# Fridge / Pantry Inventory Telegram Bot

[![tests](https://github.com/eleana-tan/digi-fridge/actions/workflows/tests.yml/badge.svg)](https://github.com/eleana-tan/digi-fridge/actions/workflows/tests.yml)

A Telegram bot that maintains a running inventory of what's in your fridge/pantry
from plain-language messages ("bought milk and eggs", "used the last of the
cheese", "what's expiring soon?"), tracks expiry dates, and proactively reminds
you about items about to go bad.

This is a working, runnable project — not a mockup.

## Design at a glance

The parsing layer and the Telegram layer are fully decoupled, so you can test
parsing/logic without a live Telegram connection.

| Module | Responsibility | Telegram-aware? | Network? |
| --- | --- | --- | --- |
| `fridge/config.py` | Settings + tiny `.env` loader | no | no |
| `fridge/models.py` | Shared dataclasses | no | no |
| `fridge/db.py` | SQLite storage, migrations, CRUD, action log | no | no |
| `fridge/parser.py` | Message → structured action (LLM **or** rule-based) | no | only LLM path |
| `fridge/actions.py` | Execute action against DB, build reply | no | no |
| `fridge/reminders.py` | Find expiring items + notify | delivery only | no |
| `fridge/bot.py` | Telegram wiring / entrypoint | **yes** | yes |
| `fridge/cli.py` | Offline pipeline tester (no bot token needed) | no | only LLM path |

The parser has two implementations behind one interface:

- **`LLMParser`** — uses the OpenAI API. Its client is injectable, so unit tests
  pass a stub returning canned JSON (no network).
- **`RuleBasedParser`** — dependency-free heuristics. Used automatically when
  `OPENAI_API_KEY` is not set, so the app runs and is testable fully offline.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env
```

`.env` values:

- `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather). Required to run the bot.
- `OPENAI_API_KEY` — optional. If set, real LLM parsing is used; otherwise the
  built-in rule-based parser is used.
- `OPENAI_MODEL` — defaults to `gpt-4o-mini`.
- `DB_PATH` — SQLite file, defaults to `fridge.db`.
- `EXPIRY_REMINDER_DAYS` — reminder window, defaults to `2`.
- `REMINDER_TIME` — daily reminder time `HH:MM` in the server's local timezone,
  defaults to `09:00`.

## Dependencies

Only the libraries you specified, plus their standard extras:

- `python-telegram-bot[job-queue]` — the `job-queue` extra provides the daily
  reminder scheduler (APScheduler under the hood).
- `openai` — only needed if you set `OPENAI_API_KEY`.
- SQLite via the Python standard library (`sqlite3`).
- Tests use the standard-library `unittest` (no pytest).

---

## Verifying each feature, in build order

Do these in order. Don't move on to step N+1 until step N works.

### Run the automated tests first (covers steps 2, 3, 4, 5, 6 offline)

```bash
python -m unittest discover -s tests -v
```

Expect `OK` with 46 tests. This exercises the DB layer, the parser (pure JSON
transform + stubbed LLM client + rule-based parser), the action layer, and the
reminder-building logic — all without Telegram or a network.

### Step 1 — Telegram skeleton that echoes messages (no LLM, no DB)

```bash
ECHO_MODE=1 python run.py
```

Then in Telegram, message your bot:

- Send `/start` → you get the welcome text.
- Send `hello there` → bot replies `You said: hello there`.

Confirmed working = you see the echo reply. Stop the bot (Ctrl+C) before step 4.

### Step 2 — Database layer (CRUD, migrations, action log)

No Telegram needed. Use the offline CLI (it creates/migrates the DB on first run):

```bash
python -m fridge.cli --db /tmp/fridge_test.db "bought 2 cartons of milk and a dozen eggs"
python -m fridge.cli --db /tmp/fridge_test.db "what do I have?"
```

Expect the first to report `Added 2 cartons milk.` / `Added 12 eggs.` and the
second to list them. Inspect the raw tables directly if you like:

```bash
sqlite3 /tmp/fridge_test.db "SELECT item_name, item_qty, unit FROM inventory;"
sqlite3 /tmp/fridge_test.db "SELECT raw_message, parsed_action FROM action_log;"
```

Also run the DB unit tests: `python -m unittest tests.test_db -v`.

### Step 3 — Parsing layer (isolated, unit-testable, no Telegram)

```bash
python -m unittest tests.test_parser -v
```

To see parsing on your own messages (rule-based unless `OPENAI_API_KEY` is set),
use `--show-parse`:

```bash
python -m fridge.cli --show-parse "used the last of the cheese"
```

You'll see the structured `ParsedAction` JSON, then the reply.

### Step 4 — Wire parsing → DB → confirmation (live bot)

Make sure `TELEGRAM_BOT_TOKEN` is set in `.env`, then:

```bash
python run.py
```

In Telegram, send:

- `bought 2 cartons of milk and a dozen eggs` → `Added 2 cartons milk.` / `Added 12 eggs.`
- `used the last of the milk` → `Removed all milk.`

(If `OPENAI_API_KEY` is unset you'll see a log line saying the rule-based parser
is in use — that's expected and still works.)

### Step 5 — Daily expiry reminders

The bot schedules a daily job at `REMINDER_TIME`. To verify the logic without
waiting a day, run the reminder unit tests:

```bash
python -m unittest tests.test_reminders -v
```

To see a real reminder end-to-end, add an item expiring today/tomorrow via the
live bot, then temporarily set `REMINDER_TIME` to a minute or two ahead of your
server's clock and restart `python run.py`. When that time hits, the bot messages
you the expiring items. (You must have messaged the bot at least once so it knows
your chat id.)

### Step 6 — Query support

With the live bot running (or via `python -m fridge.cli`):

- `what do I have?` → lists your inventory.
- `what's expiring soon?` → lists items expiring within the next few days.
- `do I have milk?` → `Yes, you have: ...` or `No, you don't have any milk.`

## Project layout

```
fridge/
  __init__.py      package overview
  config.py        settings + .env loader
  models.py        dataclasses (ItemSpec, ParsedAction, InventoryItem)
  db.py            SQLite storage, migrations, CRUD, action log
  parser.py        LLMParser (injectable client) + RuleBasedParser
  actions.py       execute ParsedAction -> reply text
  reminders.py     build_reminders (pure) + send_daily_reminders (Telegram)
  bot.py           Telegram handlers + entrypoint
  cli.py           offline pipeline tester
tests/             unittest suite (offline)
run.py             convenience entrypoint
requirements.txt
.env.example
```

## Non-goals (this pass)

No shared/multi-user households, no recipe suggestions, no photo/receipt
scanning, no auth beyond the Telegram chat id.
