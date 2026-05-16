# Remora — Архитектура

**Дата:** 2026-05-09
**Версия:** 1.0

---

## 1. СТРУКТУРА ПРОЕКТА

```
remora/
│
├── src/                    # Исходный код
│   ├── core/               # Ядро Remora
│   │   ├── remora.py       # Главный демон (оркестратор)
│   │   ├── scanner.py      # Сканер ленты Nostr
│   │   ├── evaluator.py    # LLM-оценщик
│   │   ├── generator.py    # Генератор ответов
│   │   ├── publisher.py    # Публикатор (обёртка над nostr_adapter_v3)
│   │   ├── tracker.py      # Трекер диалогов
│   │   └── memory.py       # Контекстная память (БД)
│   └── adapters/           # Симлинки + обёртки над Remora
│       └── __init__.py     # Инициализация путей к Remora
│
├── docs/                   # Документация
│   ├── SPECIFICATION.md    # Полная спека
│   ├── ARCHITECTURE.md     # Этот файл
│   ├── INHERITANCE.md      # Что наследуем из Remora
│   ├── ROADMAP.md          # План развития
│   └── IMPLEMENTATION_PLAN.md  # План реализации V1
│
├── config/
│   └── config.yaml         # Конфигурация
│
├── data/
│   └── remora.db           # SQLite БД Remora
│
├── logs/
│   └── remora.log          # Логи
│
├── assets/
│   ├── avatar.png          # Аватар Remora
│   └── banner.png          # Баннер профиля
│
└── README.md               # Общий обзор
```

---

## 2. ДИАГРАММА ПОТОКОВ

```
                        ┌──────────────┐
                        │   Nostr SDK   │
                        │ (50+ релеев)  │
                        └──────┬───────┘
                               │ kind:1
                               ▼
┌──────────────────────────────────────────────────────┐
│                   remora.py (MAIN)                    │
│                                                       │
│  while True:                                          │
│    scan() → evaluare() → generate() → publish()       │
│    sleep(120)  # цикл каждые 2 минуты                 │
│                                                       │
│  Параллельно:                                         │
│    tracker() — каждые 10 мин проверка тредов          │
└──────────────────────────────────────────────────────┘
        │            │              │              │
        ▼            ▼              ▼              ▼
   ┌────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
   │scanner │ │evaluator │ │generator │ │publisher │
   │.py     │ │.py       │ │.py       │ │.py       │
   └───┬────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘
       │           │            │             │
       ▼           ▼            ▼             ▼
   ┌────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
   │relay   │ │LLM       │ │LLM       │ │nostr     │
   │orchest │ │(Mistral) │ │(Mistral) │ │adapter_v3│
   │rator   │ │          │ │+tone     │ │          │
   └────────┘ └──────────┘ └──────────┘ └──────────┘
        │                                    │
        ▼                                    ▼
   ┌────────┐                         ┌──────────┐
   │Remora  │                         │50+ relays│
   │релеи   │                         │(multipub)│
   └────────┘                         └──────────┘
```

---

## 3. БД REMORA

```sql
-- Основная таблица: все диалоги
CREATE TABLE remora_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,           -- root post ID (начало треда)
    parent_post_id TEXT NOT NULL,      -- пост, на который ответили
    remora_reply_id TEXT,              -- event ID ответа Remora
    author_pubkey TEXT NOT NULL,       -- автор родительского поста
    author_nip05 TEXT,                 -- NIP-05 автора (если есть)
    reply_text TEXT,                   -- текст ответа Remora
    mode TEXT DEFAULT 'short',         -- short/medium/deep
    tone_used TEXT,                    -- какой тон использован
    created_at TIMESTAMP,
    closed_at TIMESTAMP,
    status TEXT DEFAULT 'active'       -- active/closed/blacklisted
);

-- Реакции на ответы Remora
CREATE TABLE remora_reactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER REFERENCES remora_conversations(id),
    reaction_type TEXT,                -- reply/like/zap
    reaction_from TEXT,                -- pubkey того, кто отреагировал
    reaction_content TEXT,             -- текст ответа (если reply)
    created_at TIMESTAMP
);

-- Статистика по авторам
CREATE TABLE remora_author_profiles (
    pubkey TEXT PRIMARY KEY,
    nip05 TEXT,
    followers INTEGER DEFAULT 0,
    total_replies INTEGER DEFAULT 0,   -- сколько раз Remora ответила
    total_reactions INTEGER DEFAULT 0, -- сколько реакций на ответы
    score REAL DEFAULT 0.5,           -- репутация (0-1)
    last_interaction TIMESTAMP,
    is_blacklisted INTEGER DEFAULT 0
);

-- Blacklist
CREATE TABLE remora_blacklist (
    pubkey TEXT PRIMARY KEY,
    reason TEXT,
    created_at TIMESTAMP
);

-- Статистика тонов
CREATE TABLE remora_tone_stats (
    tone TEXT PRIMARY KEY,
    uses INTEGER DEFAULT 0,
    reactions INTEGER DEFAULT 0,       -- суммарные реакции на этот тон
    score REAL DEFAULT 0.5
);

-- Rate-limiter
CREATE TABLE remora_rate_limiter (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    published_at TIMESTAMP,            -- когда опубликован ответ
    delay_seconds INTEGER              -- сколько секунд прошло с предыдущего
);
```

---

## 4. ЗАВИСИМОСТИ

### Прямые (из Remora, дочитываются через PYTHONPATH)

```
/home/agent/data/agents/remora_v2/remora_v7/
├── src/
│   ├── adapters/
│   │   └── nostr_adapter_v3.py    # multipub + reply
│   ├── core/
│   │   ├── relay_orchestrator.py   # управление релеями
│   │   ├── relay_health_monitor.py # монитор здоровья
│   │   ├── relay_cache.py         # кэш
│   │   ├── tone_engine_v2.py      # стили
│   │   └── nostr_relay_tester.py  # тестер
│   ├── pipeline/
│   │   └── generator.py           # LLM-вызов (если понадобится)
│   └── main_v8.py                 # LLM-конфиг
```

### Библиотеки (уже установлены)

- `nostr-sdk` (0.44.2) — Full-featured Nostr SDK
- `requests` — HTTP-запросы
- `sqlite3` — БД (встроенная)

### Установить дополнительно

- Не требуется. Всё уже есть в окружении Remora.

---

## 5. ПРОЦЕССЫ

| Процесс | PID файл | Период | Назначение |
|---------|----------|--------|-----------|
| `python3 -u src/core/remora.py` | `remora.pid` | main loop (120с) | Сканирование → оценка → генерация → публикация |
| Встроенный трекер | — | каждые 10 мин | Проверка тредов, реакций |

**Запуск:** `start.sh` (авторестарт при падении)
```bash
#!/bin/bash
cd /home/agent/data/projects/remora
PYTHONPATH="/home/agent/data/agents/remora_v2/remora_v7/src:\
/home/agent/data/agents/remora_v2/remora_v7/src/core:\
/home/agent/data/agents/remora_v2/remora_v7/src/adapters:\
/home/agent/data/agents/remora_v2/remora_v7/src/core/observation:\
$PYTHONPATH" \
exec python3 -u src/core/remora.py >> logs/remora.log 2>&1
```
