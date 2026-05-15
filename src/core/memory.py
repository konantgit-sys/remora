"""
Remora — Memory Layer.
SQLite БД: диалоги, авторы, blacklist, тона, rate-limiter.
"""
import sqlite3
import os
import json
import logging
import random
from datetime import datetime, timezone

logger = logging.getLogger("remora.memory")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(BASE_DIR, "data", "remora.db")

ALL_TONES = [
    "playful", "deadpan", "absurdist", "wholesome",
    "analytical", "cynical", "mystical", "blunt",
]


class Memory:
    """Контекстная память Remora. Хранит всё в SQLite."""

    def __init__(self, db_path: str = None):
        self.db = db_path or DB_PATH
        os.makedirs(os.path.dirname(self.db), exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS remora_conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    parent_post_id TEXT NOT NULL,
                    remora_reply_id TEXT,
                    author_pubkey TEXT NOT NULL,
                    author_nip05 TEXT,
                    reply_text TEXT,
                    mode TEXT DEFAULT 'short',
                    tone_used TEXT,
                    created_at TIMESTAMP DEFAULT (datetime('now')),
                    closed_at TIMESTAMP,
                    status TEXT DEFAULT 'active',
                    viral INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS remora_reactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER REFERENCES remora_conversations(id),
                    reaction_type TEXT,
                    reaction_from TEXT,
                    reaction_content TEXT,
                    event_id TEXT,
                    created_at TIMESTAMP DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS remora_author_profiles (
                    pubkey TEXT PRIMARY KEY,
                    nip05 TEXT,
                    followers INTEGER DEFAULT 0,
                    total_replies INTEGER DEFAULT 0,
                    total_reactions INTEGER DEFAULT 0,
                    score REAL DEFAULT 0.5,
                    last_interaction TIMESTAMP,
                    is_blacklisted INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS remora_blacklist (
                    pubkey TEXT PRIMARY KEY,
                    reason TEXT,
                    created_at TIMESTAMP DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS remora_tone_stats (
                    tone TEXT PRIMARY KEY,
                    uses INTEGER DEFAULT 0,
                    reactions INTEGER DEFAULT 0,
                    score REAL DEFAULT 0.5,
                    last_used TIMESTAMP DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS remora_rate_limiter (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    published_at TIMESTAMP DEFAULT (datetime('now')),
                    delay_seconds INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS remora_whitelist (
                    pubkey TEXT PRIMARY KEY,
                    reason TEXT,
                    first_seen TIMESTAMP DEFAULT (datetime('now')),
                    last_interaction TIMESTAMP DEFAULT (datetime('now')),
                    total_replies INTEGER DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_conv_active ON remora_conversations(status);
                CREATE INDEX IF NOT EXISTS idx_conv_thread ON remora_conversations(thread_id);
                CREATE INDEX IF NOT EXISTS idx_conv_author ON remora_conversations(author_pubkey);
                CREATE INDEX IF NOT EXISTS idx_conv_parent ON remora_conversations(parent_post_id);
                CREATE INDEX IF NOT EXISTS idx_reactions_conv ON remora_reactions(conversation_id);
            """)
            conn.commit()
            logger.info(f"Memory DB ready: {self.db}")
        except Exception as e:
            logger.error(f"Memory DB init failed: {e}")
            raise
        finally:
            conn.close()

    # ─── Conversations ───

    def save_conversation(self, thread_id: str, parent_post_id: str, author_pubkey: str,
                          reply_text: str = "", mode: str = "short", tone_used: str = "",
                          author_nip05: str = "", remora_reply_id: str = "") -> int:
        """Сохраняет новый диалог. Возвращает id."""
        conn = sqlite3.connect(self.db)
        try:
            cur = conn.execute(
                """INSERT INTO remora_conversations
                   (thread_id, parent_post_id, author_pubkey, reply_text, mode, tone_used, author_nip05, remora_reply_id)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (thread_id, parent_post_id, author_pubkey, reply_text, mode, tone_used, author_nip05, remora_reply_id)
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def get_active_threads(self, max_age_hours: int = 48) -> list:
        """Активные треды (статус active, не старше N часов)."""
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT * FROM remora_conversations
                   WHERE status = 'active'
                   AND created_at > datetime('now', ?)
                   ORDER BY created_at DESC""",
                (f"-{max_age_hours} hours",)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_conversation(self, conv_id: int) -> dict:
        """Возвращает диалог по id."""
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM remora_conversations WHERE id=?", (conv_id,)
            ).fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()

    def close_thread(self, conv_id: int):
        """Закрывает тред."""
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE remora_conversations SET status='closed', closed_at=datetime('now') WHERE id=?",
            (conv_id,)
        )
        conn.commit()
        conn.close()

    def mark_viral(self, conv_id: int):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE remora_conversations SET viral=1 WHERE id=?", (conv_id,))
        conn.commit()
        conn.close()

    def boost_viral_author(self, conv_id: int, viral_threshold: int = 3):
        """
        Апгрейдит автора если пост получил много реакций.
        Поднимает author score на 0.1 за каждую реакцию свыше threshold.
        """
        conn = sqlite3.connect(self.db)
        try:
            conv = conn.execute(
                "SELECT author_pubkey FROM remora_conversations WHERE id=?", (conv_id,)
            ).fetchone()
            if not conv:
                return

            author_pk = conv[0]
            reaction_count = conn.execute(
                "SELECT COUNT(*) FROM remora_reactions WHERE conversation_id=?", (conv_id,)
            ).fetchone()[0]

            if reaction_count >= viral_threshold:
                bonus = min(0.3, (reaction_count - viral_threshold) * 0.05)  # Max +0.3
                self.update_author_score(author_pk, bonus)
                logger.info(f"🔥 Viral boost: {author_pk[:16]}... +{bonus:.2f} (reactions: {reaction_count})")
        finally:
            conn.close()

    # ─── Duplicates ───

    def is_duplicate(self, post_id: str) -> bool:
        """Проверяли ли уже этот пост (parent_post_id)?"""
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT 1 FROM remora_conversations WHERE parent_post_id=? LIMIT 1",
                (post_id,)
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    # ─── Author profiles ───

    def get_author_score(self, pubkey: str) -> float:
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT score FROM remora_author_profiles WHERE pubkey=?", (pubkey,)
            ).fetchone()
            return row[0] if row else 0.5
        finally:
            conn.close()

    def update_author_score(self, pubkey: str, delta: float, nip05: str = "", followers: int = 0):
        conn = sqlite3.connect(self.db)
        try:
            existing = conn.execute(
                "SELECT score, total_replies FROM remora_author_profiles WHERE pubkey=?", (pubkey,)
            ).fetchone()
            if existing:
                new_score = max(0.0, min(1.0, existing[0] + delta))
                conn.execute(
                    """UPDATE remora_author_profiles
                       SET score=?, total_replies=total_replies+1, last_interaction=datetime('now')
                       WHERE pubkey=?""",
                    (new_score, pubkey)
                )
            else:
                conn.execute(
                    """INSERT INTO remora_author_profiles (pubkey, nip05, followers, score, total_replies, last_interaction)
                       VALUES (?,?,?,?,1,datetime('now'))""",
                    (pubkey, nip05 or "", followers, clamp(0.5 + delta, 0, 1))
                )
            conn.commit()
        finally:
            conn.close()

    # ─── Blacklist ───

    def is_blacklisted(self, pubkey: str) -> bool:
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT 1 FROM remora_blacklist WHERE pubkey=? LIMIT 1", (pubkey,)
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def add_blacklist(self, pubkey: str, reason: str = "evaluator D"):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO remora_blacklist (pubkey, reason) VALUES (?,?)",
                (pubkey, reason)
            )
            conn.commit()
            logger.warning(f"Blacklisted {pubkey[:16]}: {reason}")
        finally:
            conn.close()

    # ─── Whitelist ───

    def add_to_whitelist(self, pubkey: str, reason: str = "interaction"):
        """Добавляет автора в whitelist (или обновляет счётчик, если уже есть)."""
        conn = sqlite3.connect(self.db)
        try:
            existing = conn.execute(
                "SELECT total_replies FROM remora_whitelist WHERE pubkey=?", (pubkey,)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE remora_whitelist
                       SET total_replies=total_replies+1, last_interaction=datetime('now')
                       WHERE pubkey=?""",
                    (pubkey,)
                )
                logger.info(f"Whitelist updated: {pubkey[:16]} ({existing[0]+1} interactions)")
            else:
                conn.execute(
                    "INSERT INTO remora_whitelist (pubkey, reason, first_seen, last_interaction) VALUES (?,?,datetime('now'),datetime('now'))",
                    (pubkey, reason)
                )
                logger.info(f"Whitelist added: {pubkey[:16]} ({reason})")
            conn.commit()
        finally:
            conn.close()

    def is_whitelisted(self, pubkey: str) -> bool:
        """Проверяет, есть ли автор в whitelist."""
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT 1 FROM remora_whitelist WHERE pubkey=? LIMIT 1", (pubkey,)
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def get_whitelist_stats(self) -> dict:
        """Статистика whitelist."""
        conn = sqlite3.connect(self.db)
        try:
            total = conn.execute("SELECT COUNT(*) FROM remora_whitelist").fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM remora_whitelist WHERE last_interaction > datetime('now', '-7 days')"
            ).fetchone()[0]
            return {"total": total, "active_7d": active}
        finally:
            conn.close()

    def get_whitelisted_authors(self) -> list:
        """Возвращает список всех whitelist-авторов."""
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM remora_whitelist ORDER BY last_interaction DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ─── Tone stats (SELF-LEARNING) ───

    def _ensure_all_tones(self, conn):
        """Гарантирует, что все 8 тонов есть в БД."""
        existing = set(r[0] for r in conn.execute("SELECT tone FROM remora_tone_stats").fetchall())
        for t in ALL_TONES:
            if t not in existing:
                conn.execute(
                    "INSERT INTO remora_tone_stats (tone, uses, reactions, score, last_used) VALUES (?, 0, 0, 0.5, datetime('now'))",
                    (t,)
                )

    def get_best_tone(self) -> str:
        """
        Выбор тона: Exploration vs Exploitation.
        - 70%: лучший по score (exploitation)
        - 20%: случайный с весом (probabilistic)
        - 10%: самый непопулярный (exploration)
        """
        conn = sqlite3.connect(self.db)
        try:
            self._ensure_all_tones(conn)
            
            rows = conn.execute(
                "SELECT tone, uses, reactions, score, last_used FROM remora_tone_stats"
            ).fetchall()
            
            known_tones = {r[0]: {"uses": r[1], "reactions": r[2], "score": r[3], "last_used": r[4]} for r in rows}
            
            # 10% exploration — самый неиспользованный тон
            if random.random() < 0.1:
                unused = [t for t in ALL_TONES if known_tones.get(t, {}).get("uses", 0) == 0]
                if unused:
                    return random.choice(unused)
                sorted_by_uses = sorted(ALL_TONES, key=lambda t: known_tones.get(t, {}).get("uses", 0))
                return sorted_by_uses[0]
            
            # 20% weighted random (probabilistic)
            if random.random() < 0.2 / 0.9:
                weights = []
                for t in ALL_TONES:
                    s = known_tones.get(t, {}).get("score", 0.5)
                    if s < 0.01:
                        s = 0.01
                    weights.append(s)
                total = sum(weights)
                if total > 0:
                    r_rand = random.random() * total
                    cumulative = 0
                    for i, w in enumerate(weights):
                        cumulative += w
                        if r_rand <= cumulative:
                            return ALL_TONES[i]
            
            # 70% — лучший по score
            best = sorted(
                ALL_TONES,
                key=lambda t: known_tones.get(t, {}).get("score", 0.5),
                reverse=True
            )[0]
            return best
        finally:
            conn.close()

    def record_tone_use(self, tone: str):
        """Записывает использование тона + затухание старых."""
        conn = sqlite3.connect(self.db)
        try:
            existing = conn.execute(
                "SELECT uses, reactions, score FROM remora_tone_stats WHERE tone=?", (tone,)
            ).fetchone()
            if existing:
                uses, reactions, score = existing
                # Затухание: если тон не использовался 24ч — понижаем score на 10%
                decay = 1.0
                last_row = conn.execute(
                    "SELECT last_used FROM remora_tone_stats WHERE tone=?", (tone,)
                ).fetchone()
                if last_row and last_row[0]:
                    try:
                        last = datetime.strptime(last_row[0], "%Y-%m-%d %H:%M:%S")
                        hours_ago = (datetime.utcnow() - last).total_seconds() / 3600
                        if hours_ago > 24:
                            decay = max(0.5, 1.0 - (hours_ago / 240))  # 24ч → 0.9, 120ч → 0.5
                    except ValueError:
                        pass
                new_score = score * decay
                conn.execute(
                    "UPDATE remora_tone_stats SET uses=uses+1, score=?, last_used=datetime('now') WHERE tone=?",
                    (new_score, tone)
                )
            else:
                conn.execute(
                    "INSERT INTO remora_tone_stats (tone, uses, score, last_used) VALUES (?, 1, 0.5, datetime('now'))",
                    (tone,)
                )
            conn.commit()
        finally:
            conn.close()

    def record_tone_reaction(self, tone: str):
        """
        Увеличивает счётчик реакций для тона, пересчитывает score.
        Score = reactions / uses (выше = лучше).
        """
        conn = sqlite3.connect(self.db)
        try:
            existing = conn.execute(
                "SELECT uses, reactions, score FROM remora_tone_stats WHERE tone=?", (tone,)
            ).fetchone()
            if existing:
                uses, reactions, score = existing
                new_reactions = reactions + 1
                new_score = clamp(new_reactions / max(1, uses), 0, 1)
                conn.execute(
                    "UPDATE remora_tone_stats SET reactions=?, score=?, last_used=datetime('now') WHERE tone=?",
                    (new_reactions, new_score, tone)
                )
                logger.info(f"Tone '{tone}': score {score:.3f} -> {new_score:.3f} ({new_reactions}/{uses})")
            else:
                conn.execute(
                    "INSERT INTO remora_tone_stats (tone, uses, reactions, score) VALUES (?, 0, 1, 0.5)",
                    (tone,)
                )
            conn.commit()
        finally:
            conn.close()

    def get_tone_stats(self) -> list:
        """Возвращает статистику по всем тонам."""
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM remora_tone_stats ORDER BY score DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_conversation_context(self, conv_id: int, limit: int = 5) -> dict:
        """
        Возвращает контекст диалога для chain-диалогов:
        оригинальный пост, ответ Remora, автор.
        """
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            conv = conn.execute(
                "SELECT * FROM remora_conversations WHERE id=?", (conv_id,)
            ).fetchone()
            if not conv:
                return {}
            
            result = {
                "conv_id": conv["id"],
                "thread_id": conv["thread_id"],
                "parent_post_id": conv["parent_post_id"],
                "remora_reply_id": conv["remora_reply_id"],
                "author_pubkey": conv["author_pubkey"],
                "remora_reply": conv["reply_text"],
                "tone_used": conv["tone_used"],
                "mode": conv["mode"],
                "original_post": "",
            }
            
            # Пытаемся найти оригинальный пост
            parent_id = conv["parent_post_id"]
            if parent_id:
                prev = conn.execute(
                    """SELECT reply_text FROM remora_conversations
                       WHERE parent_post_id=? AND id < ?
                       ORDER BY id DESC LIMIT 1""",
                    (parent_id, conv_id)
                ).fetchone()
                if prev:
                    result["original_post"] = prev["reply_text"]
            
            return result
        finally:
            conn.close()

    def get_last_tone_used(self, conv_id: int) -> str:
        """Возвращает тон, который использовался в диалоге."""
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT tone_used FROM remora_conversations WHERE id=?", (conv_id,)
            ).fetchone()
            return row[0] if row and row[0] else ""
        finally:
            conn.close()

    # ─── Rate limiter ───

    def can_publish(self, min_interval_sec: int = 180) -> bool:
        """Проверяет, прошло ли min_interval_sec с последней публикации."""
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT (julianday('now') - julianday(published_at)) * 86400 FROM remora_rate_limiter ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return True
            return row[0] >= min_interval_sec
        finally:
            conn.close()

    def record_publish(self):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO remora_rate_limiter (published_at) VALUES (datetime('now'))"
        )
        conn.commit()
        conn.close()

    # ─── Reactions ───

    def save_reaction(self, conv_id: int, rtype: str, rfrom: str, rcontent: str = "", event_id: str = ""):
        """
        Сохраняет реакцию + автоматически обновляет tone stats,
        если у диалога есть tone_used.
        """
        conn = sqlite3.connect(self.db)
        try:
            # Сохраняем реакцию
            conn.execute(
                """INSERT INTO remora_reactions (conversation_id, reaction_type, reaction_from, reaction_content, event_id)
                   VALUES (?,?,?,?,?)""",
                (conv_id, rtype, rfrom, rcontent, event_id)
            )
            
            # Обновляем tone stats — получаем тон из диалога
            row = conn.execute(
                "SELECT tone_used FROM remora_conversations WHERE id=?", (conv_id,)
            ).fetchone()
            tone = row[0] if row and row[0] else ""
            
            if tone:
                existing = conn.execute(
                    "SELECT uses, reactions FROM remora_tone_stats WHERE tone=?", (tone,)
                ).fetchone()
                if existing:
                    uses, reactions = existing
                    new_reactions = reactions + 1
                    new_score = clamp(new_reactions / max(1, uses), 0, 1)
                    conn.execute(
                        "UPDATE remora_tone_stats SET reactions=?, score=?, last_used=datetime('now') WHERE tone=?",
                        (new_reactions, new_score, tone)
                    )
                    logger.info(f"Reaction -> tone '{tone}': score {new_score:.3f} ({new_reactions}/{uses})")
                else:
                    conn.execute(
                        "INSERT INTO remora_tone_stats (tone, uses, reactions, score) VALUES (?, 0, 1, 0.5)",
                        (tone,)
                    )
            
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to save reaction: {e}")
            return False
        finally:
            conn.close()

    def get_reaction_count(self, conv_id: int) -> int:
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM remora_reactions WHERE conversation_id=?", (conv_id,)
            ).fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    # ─── Stats ───

    def get_stats(self) -> dict:
        """Сводка статистики Remora."""
        conn = sqlite3.connect(self.db)
        try:
            total = conn.execute("SELECT COUNT(*) FROM remora_conversations").fetchone()[0]
            active = conn.execute("SELECT COUNT(*) FROM remora_conversations WHERE status='active'").fetchone()[0]
            reactions = conn.execute("SELECT COUNT(*) FROM remora_reactions").fetchone()[0]
            blacklisted = conn.execute("SELECT COUNT(*) FROM remora_blacklist").fetchone()[0]
            tones = conn.execute("SELECT COUNT(*) FROM remora_tone_stats WHERE uses>0").fetchone()[0]
            return {
                "total_conversations": total,
                "active_threads": active,
                "total_reactions": reactions,
                "blacklisted_authors": blacklisted,
                "active_tones": tones,
            }
        finally:
            conn.close()

    # ─── Chain-диалоги (новое) ───

    def get_candidate_chain_conversations(self, max_age_hours: int = 48) -> list:
        """
        Возвращает активные диалоги, на которые ещё не было ответов от автора.
        Для цепочки диалогов.
        """
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT c.id, c.thread_id, c.remora_reply_id, c.author_pubkey,
                          c.reply_text, c.tone_used, c.mode, c.created_at
                   FROM remora_conversations c
                   WHERE c.status = 'active'
                   AND c.created_at > datetime('now', ?)
                   AND NOT EXISTS (
                       SELECT 1 FROM remora_reactions r
                       WHERE r.conversation_id = c.id
                       AND r.reaction_type IN ('reply', 'mention')
                   )
                   ORDER BY c.created_at DESC
                   LIMIT 50""",
                (f"-{max_age_hours} hours",)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def mark_chain_reply(self, parent_conv_id: int, chain_reply_id: str):
        """Отмечает цепочку реплаев в диалоге."""
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "UPDATE remora_conversations SET remora_reply_id=? WHERE id=?",
                (chain_reply_id, parent_conv_id)
            )
            conn.commit()
            logger.info(f"Chain reply marked: conv_id={parent_conv_id}, reply_id={chain_reply_id[:16]}...")
        finally:
            conn.close()

    def get_conversation_by_reply_id(self, reply_id: str) -> dict:
        """Находит диалог по remora_reply_id (для цепочек)."""
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM remora_conversations WHERE remora_reply_id=?", (reply_id,)
            ).fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()

    # ─── Whitelist (новое) ───

    def add_to_whitelist(self, pubkey: str, reason: str = "replied_to_remora"):
        """Добавляет автора в whitelist (отвечаем всегда)."""
        conn = sqlite3.connect(self.db)
        try:
            existing = conn.execute(
                "SELECT * FROM remora_author_profiles WHERE pubkey=?", (pubkey,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE remora_author_profiles SET score=1.0 WHERE pubkey=?", (pubkey,)
                )
            else:
                conn.execute(
                    """INSERT INTO remora_author_profiles
                       (pubkey, score) VALUES (?, 1.0)""",
                    (pubkey,)
                )
            conn.commit()
            logger.info(f"Whitelist: {pubkey[:16]}... added (reason: {reason})")
        finally:
            conn.close()

    def is_whitelisted(self, pubkey: str) -> bool:
        """Проверяет если автор в whitelist (score >= 0.9)."""
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT score FROM remora_author_profiles WHERE pubkey=?", (pubkey,)
            ).fetchone()
            return row and row[0] >= 0.9
        finally:
            conn.close()

    def get_whitelisted_authors(self) -> list:
        """Возвращает всех авторов в whitelist."""
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT pubkey, score FROM remora_author_profiles WHERE score >= 0.9 ORDER BY score DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_author_count(self) -> int:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(DISTINCT author_pubkey) FROM remora_conversations").fetchone()[0]
        finally:
            conn.close()


def clamp(v, lo, hi):
    return max(lo, min(hi, v))
