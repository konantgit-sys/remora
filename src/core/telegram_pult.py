"""
Remora — Telegram Pult.
Управление Remora через Telegram-бота.
Запускается как отдельный процесс, подключается к той же БД.

Команды:
  /status — метрики (ответы, тона, чёрный список)
  /pause — приостановить цикл
  /resume — возобновить
  /tone <name> — переключить активный тон
  /stats — отчёт за 24ч
  /blacklist — показать чёрный список
  /whitelist — показать белый список
  /tone_stats — статистика тонов
  /help — список команд
"""
import logging
import os
import sys
import json
import time
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

# Путь к проекту Remora
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("remora.telegram")

DB_PATH = os.path.join(_PROJECT_ROOT, "data", "remora.db")
PAUSE_FLAG = os.path.join(_PROJECT_ROOT, "data", "pause.flag")
CONFIG_PATH = os.path.join(_PROJECT_ROOT, "config", "config.yaml")

# ── Telegram Bot ──
# Вставь сюда токен от @BotFather
# Или через переменную окружения: export REMORA_BOT_TOKEN="..."
BOT_TOKEN = os.environ.get("REMORA_BOT_TOKEN", "ВАШ_ТОКЕН_СЮДА")


class RemoraDB:
    """Доступ к БД Remora (только чтение + управление флагами)."""

    def __init__(self, db_path: str = DB_PATH):
        self._db = sqlite3.connect(db_path, check_same_thread=False)

    def get_status(self) -> dict:
        cur = self._db.execute("SELECT COUNT(*) FROM remora_conversations")
        total = cur.fetchone()[0]
        cur = self._db.execute("SELECT COUNT(*) FROM remora_conversations WHERE status='active'")
        active = cur.fetchone()[0]
        cur = self._db.execute("SELECT COUNT(*) FROM remora_blacklist")
        blacklist = cur.fetchone()[0]
        cur = self._db.execute("SELECT COUNT(*) FROM remora_whitelist")
        whitelist = cur.fetchone()[0]
        cur = self._db.execute("SELECT COUNT(*) FROM remora_reactions")
        reactions = cur.fetchone()[0]
        # Последняя активность
        cur = self._db.execute("SELECT created_at FROM remora_conversations ORDER BY created_at DESC LIMIT 1")
        last_row = cur.fetchone()
        last_reply = last_row[0] if last_row else "никогда"

        paused = os.path.exists(PAUSE_FLAG)

        return {
            "total_conversations": total,
            "active_threads": active,
            "blacklisted": blacklist,
            "whitelisted": whitelist,
            "total_reactions": reactions,
            "last_reply": last_reply,
            "paused": paused,
        }

    def get_tone_stats(self) -> list:
        cur = self._db.execute("""
            SELECT tone, uses, reactions, score 
            FROM remora_tone_stats 
            ORDER BY uses DESC
        """)
        return [{"tone": r[0], "uses": r[1], "reactions": r[2], "score": r[3]} for r in cur.fetchall()]

    def get_blacklist(self, limit: int = 10) -> list:
        cur = self._db.execute("""
            SELECT pubkey, reason, created_at 
            FROM remora_blacklist 
            ORDER BY created_at DESC 
            LIMIT ?
        """, [limit])
        return [{"pubkey": r[0][:16] + "...", "reason": r[1], "when": r[2]} for r in cur.fetchall()]

    def get_whitelist(self, limit: int = 10) -> list:
        cur = self._db.execute("""
            SELECT pubkey, reason, total_replies 
            FROM remora_whitelist 
            ORDER BY total_replies DESC 
            LIMIT ?
        """, [limit])
        return [{"pubkey": r[0][:16] + "...", "reason": r[1], "replies": r[2]} for r in cur.fetchall()]

    def get_recent_replies(self, limit: int = 5) -> list:
        cur = self._db.execute("""
            SELECT author_pubkey, reply_text, tone_used, mode, created_at 
            FROM remora_conversations 
            WHERE reply_text IS NOT NULL 
            ORDER BY created_at DESC 
            LIMIT ?
        """, [limit])
        return [{
            "author": r[0][:16] + "...",
            "text": (r[1] or "")[:60],
            "tone": r[2],
            "mode": r[3],
            "when": r[4],
        } for r in cur.fetchall()]

    def get_24h_stats(self) -> dict:
        """Статистика за последние 24 часа."""
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        cur = self._db.execute("""
            SELECT COUNT(*) FROM remora_conversations 
            WHERE created_at >= ?
        """, [since])
        replies_24h = cur.fetchone()[0]
        cur = self._db.execute("""
            SELECT tone_used, COUNT(*) as cnt 
            FROM remora_conversations 
            WHERE created_at >= ? AND tone_used IS NOT NULL
            GROUP BY tone_used 
            ORDER BY cnt DESC
        """, [since])
        tones_24h = [{"tone": r[0], "count": r[1]} for r in cur.fetchall()]
        return {"replies_24h": replies_24h, "tones_24h": tones_24h}


# ── Управление Remora ──

def pause_remora():
    """Приостановить Remora через pause.flag."""
    with open(PAUSE_FLAG, "w") as f:
        f.write(f"paused at {datetime.now(timezone.utc).isoformat()}")
    logger.info("⏸ Remora paused")
    return True


def resume_remora():
    """Возобновить Remora — удалить pause.flag."""
    if os.path.exists(PAUSE_FLAG):
        os.remove(PAUSE_FLAG)
    logger.info("▶️ Remora resumed")
    return True


def is_paused() -> bool:
    return os.path.exists(PAUSE_FLAG)


# ── Форматирование ответов ──

def format_status(db: RemoraDB) -> str:
    s = db.get_status()
    lines = [
        f"*🤖 Remora Status*",
        f"",
        f"📊 Всего ответов: {s['total_conversations']}",
        f"🟢 Активных тредов: {s['active_threads']}",
        f"⏸ Пауза: {'ДА' if s['paused'] else 'НЕТ'}",
        f"",
        f"⚫ Чёрный список: {s['blacklisted']}",
        f"⚪ Белый список: {s['whitelisted']}",
        f"❤️ Реакций: {s['total_reactions']}",
        f"",
        f"🕐 Последний ответ: {s['last_reply']}",
    ]
    return "\n".join(lines)


def format_tone_stats(db: RemoraDB) -> str:
    tones = db.get_tone_stats()
    if not tones:
        return "📊 Нет данных по тонам"
    lines = ["*🎭 Статистика тонов:*", ""]
    for t in tones:
        emoji = {"deadpan": "😐", "playful": "😏", "analytical": "🧐",
                 "blunt": "😤", "absurdist": "🌀", "wholesome": "😇",
                 "mystical": "🔮", "cynical": "😈"}.get(t["tone"], "🎯")
        lines.append(f"{emoji} *{t['tone']}* — {t['uses']} раз, рейтинг {t['score']:.2f}")
    return "\n".join(lines)


def format_24h_stats(db: RemoraDB) -> str:
    stats = db.get_24h_stats()
    lines = [f"*📈 Статистика за 24 часа*", f"", f"Ответов: {stats['replies_24h']}"]
    if stats["tones_24h"]:
        lines.append("")
        lines.append("*Тоны:*")
        for t in stats["tones_24h"]:
            bar = "▰" * min(t["count"], 10) + "▱" * max(0, 10 - min(t["count"], 10))
            lines.append(f"  {t['tone']}: {bar} {t['count']}")
    return "\n".join(lines)


def format_recent(db: RemoraDB) -> str:
    replies = db.get_recent_replies()
    if not replies:
        return "📭 Нет последних ответов"
    lines = ["*🕐 Последние ответы:*", ""]
    for r in replies:
        lines.append(f"→ *{r['author']}* [{r['tone']}/{r['mode']}]:")
        lines.append(f"  _{r['text']}_")
        lines.append(f"  🕐 {r['when']}")
        lines.append("")
    return "\n".join(lines)


# ── Telegram Bot Handler ──

def handle_message(text: str, db: RemoraDB) -> str:
    """Обрабатывает команду и возвращает ответ."""
    cmd = text.strip().lower().split()

    if not cmd:
        return "Используй /help для списка команд"

    if cmd[0] == "/status":
        return format_status(db)

    elif cmd[0] == "/pause":
        if is_paused():
            return "⏸ Уже на паузе"
        pause_remora()
        return "✅ Remora приостановлена. Для возобновления: /resume"

    elif cmd[0] == "/resume":
        if not is_paused():
            return "▶️ Remora уже работает"
        resume_remora()
        return "✅ Remora возобновлена"

    elif cmd[0] == "/tone":
        if len(cmd) < 2:
            return "Укажи тон: /tone deadpan (или playful, analytical, blunt, absurdist, wholesome, mystical, cynical)"
        tone = cmd[1].lower()
        valid_tones = ["deadpan", "playful", "analytical", "blunt", "absurdist", "wholesome", "mystical", "cynical"]
        if tone not in valid_tones:
            return f"❌ Неверный тон. Доступны: {', '.join(valid_tones)}"
        # Сохраняем предпочтение тона в файл
        tone_file = os.path.join(os.path.dirname(DB_PATH), "preferred_tone.txt")
        with open(tone_file, "w") as f:
            f.write(tone)
        return f"✅ Предпочтительный тон: *{tone}*"

    elif cmd[0] == "/stats":
        return format_24h_stats(db)

    elif cmd[0] == "/recent":
        return format_recent(db)

    elif cmd[0] == "/blacklist":
        bl = db.get_blacklist(10)
        if not bl:
            return "⚫ Чёрный список пуст"
        lines = ["*⚫ Чёрный список (10):*", ""]
        for b in bl:
            lines.append(f"• `{b['pubkey']}` — {b['reason']}")
        return "\n".join(lines)

    elif cmd[0] == "/whitelist":
        wl = db.get_whitelist(10)
        if not wl:
            return "⚪ Белый список пуст"
        lines = ["*⚪ Белый список (10):*", ""]
        for w in wl:
            lines.append(f"• `{w['pubkey']}` — {w['replies']} ответов ({w['reason']})")
        return "\n".join(lines)

    elif cmd[0] == "/tone_stats":
        return format_tone_stats(db)

    elif cmd[0] in ("/help", "/start"):
        return (
            "*🤖 Remora Pult — команды:*\n\n"
            "/status — текущие метрики\n"
            "/pause — приостановить Remora\n"
            "/resume — возобновить\n"
            "/tone <name> — установить тон\n"
            "/stats — отчёт за 24ч\n"
            "/recent — последние ответы\n"
            "/blacklist — чёрный список\n"
            "/whitelist — белый список\n"
            "/tone_stats — статистика тонов\n"
            "/help — это сообщение"
        )

    else:
        return f"❌ Неизвестная команда: {cmd[0]}. Используй /help"


# ── Запуск ──

def run_bot():
    """Запускает Telegram-бота (long-polling)."""
    if BOT_TOKEN == "ВАШ_ТОКЕН_СЮДА":
        logger.error("❌ Токен не установлен!")
        logger.info("Настройка:")
        logger.info("  1. Создай бота у @BotFather")
        logger.info("  2. Получи токен")
        logger.info("  3. Запиши в переменную: export REMORA_BOT_TOKEN='ваш_токен'")
        logger.info("  Или впиши токен в этот файл (BOT_TOKEN = 'ваш_токен')")
        return

    try:
        import telebot
    except ImportError:
        logger.info("📦 Устанавливаю pyTelegramBotAPI...")
        import subprocess
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "pyTelegramBotAPI", "--break-system-packages"]
        )
        import telebot

    db = RemoraDB()
    bot = telebot.TeleBot(BOT_TOKEN)

    @bot.message_handler(func=lambda m: True)
    def handler(message):
        try:
            response = handle_message(message.text, db)
            bot.reply_to(message, response, parse_mode="Markdown")
            logger.info(f"TG cmd: {message.text}")
        except Exception as e:
            logger.error(f"TG handler error: {e}")
            try:
                bot.reply_to(message, f"❌ Ошибка: {e}")
            except:
                pass

    logger.info("🤖 Telegram pult запущен. Ожидание команд...")
    bot.infinity_polling(timeout=30, long_polling_timeout=10)


if __name__ == "__main__":
    run_bot()
