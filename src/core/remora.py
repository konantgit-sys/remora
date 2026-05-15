"""
Remora — Daemon V2.
Главный цикл: scan → evaluate → generate → publish.
+ трекер в фоне.
Полностью самостоятельный.
PID-файл для защиты от плодящихся демонов.
Автономный: start.sh + init.sh для автовосстановления.
Конфиг: config/config.yaml (переопределяет встроенные defaults).
"""
import logging
import time
import sys
import os
import signal
import sqlite3
from datetime import datetime, timezone

# Добавляем корень проекта в PYTHONPATH
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

PID_FILE = os.path.join(_PROJECT_ROOT, "remora.pid")


def _check_pid_file() -> bool:
    """
    Проверяет PID-файл. Если процесс с этим PID жив — exit.
    Если PID-файл есть, но процесс мёртв — удаляет его (stale lock).
    """
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            # Проверяем, жив ли процесс
            os.kill(old_pid, 0)
            # Процесс жив — это дубликат
            print(f"[FATAL] Remora уже запущена (PID {old_pid}). Завершаюсь.")
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            # Stale PID — процесс мёртв, удаляем
            try:
                os.remove(PID_FILE)
                print("[LOCK] Stale PID file removed")
            except:
                pass
    # Пишем свой PID
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
return True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger("remora")

from src.core.remora_adapter import RemoraAdapter
from src.core.memory import Memory
from src.core.scanner import Scanner
from src.core.evaluator import Evaluator
from src.core.generator import Generator
from src.core.publisher import Publisher
from src.core.tracker import Tracker

# Defaults — переопределяются config.yaml
CONFIG = {
    "nsec": "nsec1c7hpxhmlpjls0gnmgcdtxu8a5f466g4njzaek8372656xfay37squsue6z",
    "npub": "npub134rgd9878v5547n3yu0dgz0mlcrpetk7hcc8ny4pxzrfdmml486qzm7zm0",
    "min_score": "B",
    "short_ratio": 0.7,
    "medium_ratio": 0.2,
    "deep_ratio": 0.1,
    "rate_limit_minutes": 3,
    "active_start_hour": 3,   # UTC — ЕКБ 08:00 (UTC+5)
    "active_end_hour": 21,    # UTC — ЕКБ 02:00 (UTC+5)
    "peak_start": 9,          # UTC — ЕКБ 14:00
    "peak_end": 15,           # UTC — ЕКБ 20:00
    "peak_multiplier": 2.0,
    "scan_interval": 120,
    "max_posts_per_cycle": 15,
    "max_replies_per_cycle": 12,
}


def load_yaml_config(path: str = None) -> dict:
    """
    Загружает config.yaml и маппит в плоский dict.
    Структура YAML: remora.evaluator.min_score → cfg["min_score"]
    """
    if path is None:
        path = os.path.join(_PROJECT_ROOT, "config", "config.yaml")

    if not os.path.exists(path):
        logger.warning(f"Config not found: {path}, using defaults")
        return {}

    try:
        import yaml
        with open(path) as f:
            raw = yaml.safe_load(f)

        if not raw or "remora" not in raw:
            logger.warning("Invalid config (no 'remora' key), using defaults")
            return {}

        r = raw["remora"]
        flat = {}

        # Ключи верхнего уровня
        for k in ("nsec", "npub", "hex_pub", "nip05", "site", "avatar", "mode"):
            if k in r:
                flat[k] = r[k]

        # scanner.*
        if "scanner" in r:
            s = r["scanner"]
            if "max_posts_per_cycle" in s:
                flat["max_posts_per_cycle"] = s["max_posts_per_cycle"]
            if "max_replies_per_cycle" in s:
                flat["max_replies_per_cycle"] = s["max_replies_per_cycle"]
            if "scan_interval" in s:
                flat["scan_interval"] = s["scan_interval"]
            if "languages" in s:
                flat["languages"] = s["languages"]

        # evaluator.*
        if "evaluator" in r:
            ev = r["evaluator"]
            for k in ("budget_per_hour", "min_score", "model"):
                if k in ev:
                    flat[k] = ev[k]

        # generator.*
        if "generator" in r:
            g = r["generator"]
            for k in ("short_ratio", "medium_ratio", "deep_ratio",
                       "rate_limit_minutes", "model", "max_retries", "retry_delay"):
                if k in g:
                    flat[k] = g[k]

        # active_hours.*
        if "active_hours" in r:
            ah = r["active_hours"]
            if "start" in ah:
                flat["active_start_hour"] = ah["start"]
            if "end" in ah:
                flat["active_end_hour"] = ah["end"]
            if "peak_start" in ah:
                flat["peak_start"] = ah["peak_start"]
            if "peak_end" in ah:
                flat["peak_end"] = ah["peak_end"]
            if "peak_multiplier" in ah:
                flat["peak_multiplier"] = ah["peak_multiplier"]

        # tracker.*
        if "tracker" in r:
            tr = r["tracker"]
            for k in ("check_interval", "thread_ttl", "continue_dialog", "viral_threshold"):
                if k in tr:
                    flat[k] = tr[k]

        # scan_interval (scanner, но маппим прямо)
        if "scanner" in r and "max_age_minutes" in r["scanner"]:
            pass  # не критично

        if "remora" in r and "scanner" in r and "max_posts_per_cycle" in r["scanner"]:
            pass  # уже выше

        logger.info(f"📋 Config loaded: {len(flat)} keys from {path}")
        return flat

    except Exception as e:
        logger.warning(f"Config load error: {e}, using defaults")
        return {}


class RemoraDaemon:
    """Главный демон Remora."""

    def __init__(self, config: dict = None):
        # V2: PID-защита от плодящихся демонов
        self._pid_guard()
        
        # Сначала загружаем YAML, потом мержим с переданным config
        yaml_cfg = load_yaml_config()
        merged = {**CONFIG, **yaml_cfg, **(config or {})}
        self.cfg = merged

        logger.info("=" * 50)
        logger.info("REMORA v1.0 — Starting up")
        logger.info("=" * 50)

        self.adapter = RemoraAdapter(nsec=self.cfg["nsec"])
        self.memory = Memory()
        self.scanner = Scanner(adapter=self.adapter, memory=self.memory, config=self.cfg)
        self.evaluator = Evaluator(memory=self.memory, config=self.cfg)
        self.generator = Generator(memory=self.memory, config=self.cfg)
        self.publisher = Publisher(adapter=self.adapter, memory=self.memory, config=self.cfg)
        self.tracker = Tracker(memory=self.memory, adapter=self.adapter, generator=self.generator, publisher=self.publisher, config=self.cfg)

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    @staticmethod
    def _pid_guard():
        """Проверяет PID-файл. Если процесс с этим PID жив — exit (анти-дубль)."""
        pid_file = PID_FILE
        if os.path.exists(pid_file):
            try:
                with open(pid_file) as f:
                    old_pid = int(f.read().strip())
                os.kill(old_pid, 0)  # Проверка: жив ли?
                # Процесс жив → это дубликат
                logger.error(f"🛑 Remora уже запущена (PID {old_pid}). Завершаюсь.")
                sys.exit(1)
            except (ProcessLookupError, ValueError):
                # Stale PID — процесс мёртв, удаляем
                try:
                    os.remove(pid_file)
                    logger.warning("🧹 Stale PID file removed")
                except:
                    pass
        # Пишем свой PID
        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))

    def run(self):
        self._running = True
        self.tracker.start()

        self._daily_report_date = None  # Отслеживаем, когда опубликован daily report

        cycle = 0
        while self._running:
            try:
                cycle += 1
                now = datetime.now(timezone.utc)
                hour = now.hour

                if not self._is_active_hour(hour):
                    logger.debug(f"Sleeping (hour {hour} UTC)")
                    self._sleep(600)
                    continue

                # V2: Pause flag (Telegram-пульт)
                _pause_path = os.path.join(_PROJECT_ROOT, "data", "pause.flag")
                if os.path.exists(_pause_path):
                    logger.info("⏸ Paused by Telegram (pause.flag)")
                    self._sleep(30)
                    continue

                # V2: Preferred tone from Telegram
                _tone_path = os.path.join(_PROJECT_ROOT, "data", "preferred_tone.txt")
                if os.path.exists(_tone_path):
                    try:
                        with open(_tone_path) as _f:
                            _pref = _f.read().strip()
                        if _pref in ("deadpan","playful","analytical","blunt","absurdist","wholesome","mystical","cynical"):
                            self.cfg["preferred_tone"] = _pref
                            logger.info(f"🎭 Tone set by Telegram: {_pref}")
                    except:
                        pass

                logger.info(f"\n{'='*40}")
                logger.info(f"🔄 Cycle #{cycle} @ {now.strftime('%H:%M:%S')} UTC")

                posts = self.scanner.scan()
                if not posts:
                    logger.info("📭 No posts")
                    self._sleep(self.cfg["scan_interval"])
                    continue

                evaluated = self.evaluator.evaluate(posts)
                worthy = self.evaluator.filter_worthy(evaluated)

                # V2: адаптивный rate limit
                self.publisher.on_cycle_start(len(worthy))

                if not worthy:
                    logger.info("📭 No worthy posts")
                    self._sleep(self.cfg["scan_interval"])
                    continue

                max_replies = self._get_max_replies(hour)
                published = 0

                for idx, candidate in enumerate(worthy[:max_replies]):
                    if not self._running:
                        break

                    # V2: rate limit проверяется внутри publisher
                    if idx > 0 and not self.publisher.can_burst():
                        logger.info(f"⏳ Burst depleted, moving on")
                        break

                    reply = self.generator.generate(candidate)
                    if reply.get("blocked"):
                        continue

                    post = candidate["post"]
                    result = self.publisher.publish(
                        text=reply["text"],
                        post_id=post.get("id", ""),
                        author_pubkey=post.get("pubkey", ""),
                        relay_url=post.get("relay_url", ""),
                    )

                    if result.get("success"):
                        self.memory.save_conversation(
                            thread_id=post.get("id", ""),
                            parent_post_id=post.get("id", ""),
                            author_pubkey=post.get("pubkey", ""),
                            reply_text=reply["text"],
                            mode=reply["mode"],
                            tone_used=reply["tone"],
                        )
                        published += 1
                        logger.info(f"✅ Reply #{published}: [{reply['mode']}/{reply['tone']}]")

                logger.info(f"Cycle #{cycle}: {len(posts)} scanned, {len(worthy)} worthy, {published} published")

                # Цепочка диалогов (ответы на реплаи)
                chain_published = self._chain_phase()
                if chain_published:
                    logger.info(f"🔗 Chain phase: {chain_published} replies published")

                self._daily_report_check()
                self._sleep(self._get_wait_time(hour))

            except Exception as e:
                logger.error(f"Cycle error: {e}", exc_info=True)
                self._sleep(30)

        self._shutdown()

    def _chain_phase(self) -> int:
        """
        Цепочка диалогов: находит реплаи на свои посты и отвечает на них.
        Возвращает количество опубликованных цепочек.
        """
        try:
            # Находим активные диалоги без ответов (кандидаты на цепочку)
            candidates = self.memory.get_candidate_chain_conversations(max_age_hours=48)
            if not candidates:
                return 0

            published = 0
            for cand in candidates[:3]:  # Макс 3 цепочки за цикл
                if not self._running:
                    break

                if not self.memory.can_publish(self.cfg["rate_limit_minutes"] * 60):
                    break

                try:
                    # Ищем реплаи на remora_reply_id (наш пост)
                    reply_id = cand.get("remora_reply_id")
                    if not reply_id:
                        continue

                    replies = self.adapter.fetch_replies_to_post(
                        reply_id, since_sec_ago=86400, limit=10
                    )
                    if not replies:
                        continue

                    # Берём первый реплай от автора оригинального поста
                    author_pk = cand.get("author_pubkey")
                    author_reply = None
                    for r in replies:
                        if r["pubkey"] == author_pk:
                            author_reply = r
                            break

                    if not author_reply:
                        continue

                    logger.info(f"🔗 Chain candidate: {author_pk[:16]}... replied to {reply_id[:16]}...")

                    # Генерируем ответ на цепочку (используем тот же тон)
                    chain_candidate = {
                        "post": author_reply,
                        "mode": cand.get("mode", "short"),
                        "tone": cand.get("tone_used", "playful"),  # Повторяем тон
                        "context": cand.get("reply_text", ""),
                    }

                    chain_reply = self.generator.generate(chain_candidate, is_chain=True)
                    if chain_reply.get("blocked"):
                        continue

                    # Публикуем цепочку
                    result = self.publisher.publish(
                        text=chain_reply["text"],
                        post_id=author_reply.get("id", ""),
                        author_pubkey=author_reply.get("pubkey", ""),
                        relay_url=author_reply.get("relay_url", ""),
                    )

                    if result.get("success"):
                        chain_event_id = result.get("event_id", "")
                        # Сохраняем цепочку в БД
                        self.memory.mark_chain_reply(cand["id"], chain_event_id)
                        logger.info(f"✅ Chain reply: [{chain_reply['mode']}/{chain_reply['tone']}]")
                        published += 1

                except Exception as e:
                    logger.warning(f"Chain error: {e}")

            return published

        except Exception as e:
            logger.error(f"Chain phase error: {e}")
            return 0

    def _daily_report_check(self):
        """
        Раз в UTC-день публикует kind:1 пост-отчёт.
        Не reply — самостоятельный пост.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._daily_report_date == today:
            return
        
        try:
            stats = self.memory.get_stats()
            total = stats.get("total_conversations", 0)
            reactions = stats.get("total_reactions", 0)
            tones = stats.get("active_tones", 0)
            
            # Берём последний диалог (лучший ответ)
            conn = sqlite3.connect(self.memory.db)
            conn.row_factory = sqlite3.Row
            best = conn.execute(
                "SELECT reply_text, mode, tone_used, created_at FROM remora_conversations ORDER BY id DESC LIMIT 3"
            ).fetchall()
            conn.close()
            
            # Формируем текст дайджеста
            lines = []
            lines.append("🐟 *Ежедневный дайджест Remora*")
            lines.append("")
            lines.append(f"Ответов за всё время: {total}")
            lines.append(f"Реакций получено: {reactions}")
            lines.append(f"Активных тонов: {tones}")
            
            if best:
                lines.append("")
                lines.append("— *Лучшие ответы дня:* —")
                for i, b in enumerate(best[:3], 1):
                    text = b["reply_text"][:80]
                    tone = b["tone_used"] or "?"
                    mode = b["mode"] or "?"
                    lines.append(f"{i}. [{mode}/{tone}] \"{text}...\"")
            
            lines.append("")
            lines.append("Remora — аналитический агент. Комментирую, не публикую.")
            lines.append("https://remora.v2.site")
            
            content = "\n".join(lines)
            
            # Публикуем как kind:1 (самостоятельный пост, не reply)
            event_id = self.adapter.publish_event(content=content, kind=1)
            if event_id:
                self._daily_report_date = today
                logger.info(f"📅 Daily report published: {event_id[:16]}...")
        except Exception as e:
            logger.warning(f"Daily report error: {e}")
    
    def _is_active_hour(self, hour: int) -> bool:
        return self.cfg["active_start_hour"] <= hour < self.cfg["active_end_hour"]

    def _get_max_replies(self, hour: int) -> int:
        base = self.cfg["max_replies_per_cycle"]
        if self.cfg["peak_start"] <= hour < self.cfg["peak_end"]:
            return int(base * self.cfg["peak_multiplier"])
        return base

    def _get_wait_time(self, hour: int) -> int:
        base = self.cfg["scan_interval"]
        if self.cfg["peak_start"] <= hour < self.cfg["peak_end"]:
            return max(int(base / self.cfg["peak_multiplier"]), 30)
        return base

    def _sleep(self, seconds: int):
        for _ in range(seconds):
            if not self._running:
                break
            time.sleep(1)

    def _handle_signal(self, signum, frame):
        logger.info(f"Signal {signum} received, shutting down...")
        self._running = False

    def _shutdown(self):
        logger.info("Shutting down Remora...")
        self.tracker.stop()
        stats = self.memory.get_stats()
        logger.info(f"Final stats: {stats}")
        # V2: очищаем PID-файл
        try:
            if os.path.exists(PID_FILE):
                with open(PID_FILE) as f:
                    stored_pid = int(f.read().strip())
                if stored_pid == os.getpid():
                    os.remove(PID_FILE)
                    logger.info("🧹 PID file cleaned")
        except:
            pass
        logger.info("Remora stopped.")


if __name__ == "__main__":
    daemon = RemoraDaemon()
    daemon.run()
