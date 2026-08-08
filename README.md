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
| `fridge/transcribe.py` | Voice audio → text (OpenAI) | no | only voice path |
| `fridge/vision.py` | Grocery/receipt photo → proposed items (OpenAI) | no | only photo path |
| `fridge/actions.py` | Execute action against DB, build reply | no | no |
| `fridge/reminders.py` | Find expiring items + notify | delivery only | no |
| `fridge/bot.py` | Telegram wiring / entrypoint | **yes** | yes |
| `fridge/cli.py` | Offline pipeline tester (no bot token needed) | no | only LLM path |

The parser has two implementations behind one interface:

- **`LLMParser`** — uses the OpenAI API. Its client is injectable, so unit tests
  pass a stub returning canned JSON (no network).
- **`RuleBasedParser`** — dependency-free heuristics. Used automatically when
  `OPENAI_API_KEY` is not set, so the app runs and is testable fully offline.

### Multiple users, groups, and attribution

Inventory lives in a **scope**:

- In a **direct message**, the scope is the user's personal fridge
  (`user:<handle>`) — private, isolated per person.
- In a **group chat**, the scope is one **shared fridge** for the group
  (`chat:<id>`), and every item records **who added it** (`user_id`). Ordinary
  list / "do we have milk?" / expiring replies do **not** show buyers. Ask
  explicitly: `who bought the milk?`, `whose eggs are these?`, or
  `what did alice buy?`.

The blocking LLM/transcription/vision calls run in worker threads
(`asyncio.to_thread`), so one user's slow request never stalls the bot for
everyone else; all database access stays on the event-loop thread, keeping
SQLite single-threaded and safe. (Schema migration `v2` adds the `scope_key`
column and backfills existing rows to each owner's personal scope.)

### Photo logging (verify + edit before save)

Send a photo of a **receipt** or your **groceries laid out**. The bot uses a
vision model (`OPENAI_VISION_MODEL`, default `gpt-4o-mini`) to extract items,
then shows a numbered draft. **Nothing is written until you confirm.**

Hybrid confirmation (buttons + text):

- Buttons: **Add all** / **Cancel**
- Or type: `yes` / `no`, `remove 2`, `remove milk`, `change 1 to 3 cartons milk`

On confirm, items go through the same add pipeline (scope + attribution).

### Voice messages

Send the bot a voice note and it transcribes it with OpenAI (Whisper by
default), echoes what it heard, then runs the transcript through the exact same
parse → DB → reply pipeline as typed text. Telegram voice notes are Opus/OGG,
which the transcription API accepts directly (no ffmpeg needed). Voice requires
`OPENAI_API_KEY`; without it, the bot politely asks you to type instead.

### Latency

Almost all response time is the OpenAI network/inference call (SQLite is
local and negligible). Levers, cheapest first:

- **Typing indicator** — the bot shows "typing…" immediately, so replies feel
  faster even when the model is thinking.
- **Non-blocking** — LLM/transcription calls run in worker threads, so users
  don't queue behind each other.
- **`PARSER_MODE=hybrid`** — the biggest win: simple messages ("bought milk",
  "what do I have?") are answered instantly by the offline rule-based parser,
  and only complex/date-bearing messages ("milk that expires Friday",
  corrections) go to the LLM.
- **Model choice** — `gpt-4o-mini` is fast and non-reasoning. If you use a
  GPT-5-family model, set `OPENAI_REASONING_EFFORT=minimal` (or `low`) to avoid
  slow hidden-reasoning tokens. `OPENAI_MAX_TOKENS` caps generation.
- **Faster transcription** — `OPENAI_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe`
  is generally quicker/cheaper than `whisper-1`.

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
- `OPENAI_TEMPERATURE` — optional; leave blank to use the model default
  (required for GPT-5-family models, which reject a custom temperature).
- `PARSER_MODE` — `llm` (default), `hybrid` (rule-based fast path + LLM
  fallback, lower latency), or `rule` (offline only). See Latency below.
- `OPENAI_REASONING_EFFORT` — for GPT-5-family models, `minimal`/`low` cuts
  latency sharply. Leave blank for non-reasoning models.
- `OPENAI_MAX_TOKENS` — optional cap on the parser's output tokens.
- `OPENAI_TRANSCRIBE_MODEL` — voice transcription model, defaults to `whisper-1`.
- `OPENAI_VISION_MODEL` — photo item-extraction model, defaults to `gpt-4o-mini`.
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

Expect `OK` with 72 tests. This exercises the DB layer, the parser (pure JSON
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
- `do I have milk?` → `Yes: ...` or `No, there's no milk here.`

### Step 7 — Group attribution (shared fridge)

Add the bot to a Telegram **group** with another person, then:

- Have user A send `bought 2 cartons of milk` and user B send `bought a dozen eggs`.
- Anyone sends `what do we have?` → shared list **without** buyer names.
- Send `who bought the milk?` → `Here's who bought milk: - 2 cartons milk — @A`.
- Send `what did alice buy?` → only Alice's items.

Offline, the DB/attribution logic is covered by
`python -m unittest tests.test_db tests.test_actions tests.test_parser -v`.

### Step 8 — Photo logging (verify + edit before saving)

Requires `OPENAI_API_KEY`. Send the bot a **photo** of a receipt or groceries:

- It replies with a numbered draft plus **Add all / Cancel** buttons.
- Edit first if needed: `remove 2`, `change 1 to 3 eggs`, then `yes` (or tap Add all).
- Tap **Cancel** / type `no` → nothing is saved.

Offline: `python -m unittest tests.test_vision tests.test_pending -v`.

## Project layout

```
fridge/
  __init__.py      package overview
  config.py        settings + .env loader
  models.py        dataclasses (ItemSpec, ParsedAction, InventoryItem)
  db.py            SQLite storage, migrations, CRUD, action log
  parser.py        LLMParser (injectable client) + RuleBasedParser
  transcribe.py    OpenAITranscriber (voice -> text)
  vision.py        OpenAIImageExtractor (photo -> proposed items)
  pending.py       draft edit/confirm helpers for photo proposals
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

Per-user inventories (DMs) and shared group fridges with per-item attribution
are supported, along with voice input and photo/receipt logging. Still out of
scope this pass: recipe suggestions, and any auth beyond the Telegram
user/chat id. Photo extraction always asks you to confirm before saving.
