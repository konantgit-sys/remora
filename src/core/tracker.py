"""
Remora — Tracker.
Мониторинг ответов на ответы Remora в реальном времени (каждые 10 мин).
Обрабатывает kind:1 (reply), kind:7 (like), kind:9735 (zap).
При обнаружении реакций — обновляет tone stats (self-learning).
"""
import logging
import time
import threading

logger = logging.getLogger("remora.tracker")


class Tracker:
    """Трекер диалогов. Проверяет реакции на ответы Remora."""

    def __init__(self, memory=None, adapter=None, generator=None, publisher=None, config: dict = None):
        self.mem = memory
        self.adapter = adapter
        self.generator = generator
        self.publisher = publisher
        self.config = config or {}
        self.check_interval = self.config.get("check_interval", 10)  # минут
        self.thread_ttl = self.config.get("thread_ttl", 120)  # минут
        self.continue_dialog = self.config.get("continue_dialog", True)
        self.viral_threshold = self.config.get("viral_threshold", 3)
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        """Запускает трекер в отдельном потоке."""
        if self._thread and self._thread.is_alive():
            logger.warning("Tracker already running")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="tracker")
        self._thread.start()
        logger.info(f"Tracker started (check every {self.check_interval} min)")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Tracker stopped")

    def _loop(self):
        """Основной цикл трекера."""
        while not self._stop.is_set():
            try:
                self._check_threads()
            except Exception as e:
                logger.warning(f"Tracker cycle error: {e}")
            for _ in range(self.check_interval * 12):
                if self._stop.is_set():
                    return
                time.sleep(5)

    def _check_threads(self):
        """Проверяет все активные треды — ищет реакции."""
        if not self.mem:
            return
        
        threads = self.mem.get_active_threads(max_age_hours=48)
        if not threads:
            return
        
        for thread in threads:
            conv_id = thread["id"]
            age_hours = self._age_hours(thread.get("created_at", ""))
            
            # Если тред старше thread_ttl — закрываем
            if age_hours * 60 > self.thread_ttl:
                self.mem.close_thread(conv_id)
                logger.info(f"Thread #{conv_id} closed (age > {self.thread_ttl} min)")
                continue
            
            # Пытаемся найти реакции через Nostr
            self._fetch_reactions(thread)
        
        logger.debug(f"Tracker: checked {len(threads)} threads")

    def _fetch_reactions(self, thread: dict):
        """
        Ищет реакции на ответ Remora через Nostr релеи.
        kind:1 = reply (chain dialog), kind:7 = like, kind:9735 = zap.
        При kind:1 от другого автора — запускает chain-диалог.
        """
        conv_id = thread["id"]
        remora_reply_id = thread.get("remora_reply_id", "")
        tone_used = thread.get("tone_used", "")
        
        if not remora_reply_id:
            return
        
        # Собираем реакции через Nostr
        found_reactions = self._query_reactions(remora_reply_id)
        
        if not found_reactions:
            return
        
        remora_pubkey_hex = self.adapter.get_pubkey_hex() if self.adapter else ""
        
        for r in found_reactions:
            # Пропускаем свои же события
            if r["pubkey"] == remora_pubkey_hex:
                continue

            # Проверяем, не обрабатывали ли уже
            if not self._is_reaction_new(r["id"], r["kind"], r["pubkey"]):
                continue

            # Сохраняем реакцию (автоматом обновляет tone stats)
            self.mem.save_reaction(
                conv_id=conv_id,
                rtype=f"kind:{r['kind']}",
                rfrom=r["pubkey"],
                rcontent=r.get("content", ""),
                event_id=r["id"],
            )
            logger.info(f"Reaction on #{conv_id}: kind:{r['kind']} from {r['pubkey'][:16]}")

            # Whitelist: если кто-то ответил (kind:1) — добавляем в whitelist
            if r["kind"] == 1:
                self.mem.add_to_whitelist(r["pubkey"], reason="replied_to_remora")
            
            # Chain-диалог: если кто-то ответил (kind:1) — продолжаем разговор
            if r["kind"] == 1 and self.continue_dialog and self.generator and self.publisher:
                # Сначала добавляем в whitelist
                if self.mem and not self.mem.is_whitelisted(r["pubkey"]):
                    self.mem.add_to_whitelist(r["pubkey"], "chain_reply")
                self._handle_chain_reply(thread, r)
        
        # Viral repost: если хит порога и ответ ещё не репостнут
        reaction_count = self.mem.get_reaction_count(conv_id)
        if reaction_count >= self.viral_threshold:
            if not thread.get("viral"):
                self.mem.mark_viral(conv_id)
                self._viral_repost(thread)
                logger.info(f"🔥 Viral thread #{conv_id}: {reaction_count} reactions (tone: {tone_used})")

    def _handle_chain_reply(self, thread: dict, reaction: dict):
        """
        Обрабатывает ответ человека на ответ Remora.
        Генерирует продолжение диалога и публикует его.
        """
        try:
            conv_id = thread["id"]
            
            # Получаем полный контекст диалога
            context = self.mem.get_conversation_context(conv_id, limit=5)
            if not context:
                logger.warning(f"Chain reply #{conv_id}: no context found")
                return
            
            original_post = context.get("original_post", "")
            remora_reply = context.get("remora_reply", "")
            tone = context.get("tone_used", "playful")
            author_pubkey = context.get("author_pubkey", "")
            
            # Генерируем продолжение
            result = self.generator.continue_conversation(
                original_post=original_post,
                remora_reply=remora_reply,
                user_reply=reaction.get("content", ""),
                tone=tone,
            )
            
            if not result or result.get("blocked"):
                logger.warning(f"Chain reply #{conv_id}: blocked or empty")
                return
            
            # Публикуем ответ
            reply_text = result["text"]
            pub_result = self.publisher.publish(
                text=reply_text,
                post_id=reaction.get("id", ""),
                author_pubkey=author_pubkey,
                relay_url=thread.get("relay_url", ""),
            )
            
            if pub_result.get("success"):
                # Сохраняем как новый диалог (продолжение)
                self.mem.save_conversation(
                    thread_id=thread.get("thread_id", ""),
                    parent_post_id=reaction.get("id", ""),
                    author_pubkey=author_pubkey,
                    reply_text=reply_text,
                    mode="chain",
                    tone_used=result.get("tone", tone),
                    remora_reply_id=pub_result.get("event_id", ""),
                )
                logger.info(f"✅ Chain dialog #{conv_id}: replied to {reaction['pubkey'][:16]}")
        except Exception as e:
            logger.error(f"Chain dialog error #{conv_id}: {e}")

    def _viral_repost(self, thread: dict):
        """
        Публикует вирусный ответ Remora как отдельный kind:1 пост.
        Чтобы ответ «выстрелил» в ленту, а не остался под чужим постом.
        """
        if not self.publisher:
            return
        try:
            reply_text = thread.get("reply_text", "")
            tone = thread.get("tone_used", "playful")
            if not reply_text:
                return
            
            # Формируем standalone-пост
            content = f"🐟 *Viral Remora*\n\n{reply_text}"
            tags = [
                ["t", "remora"],
                ["t", "bitcoin"],
                ["t", "nostr"],
            ]
            
            result = self.publisher.publish_standalone(content, tags=tags)
            if result.get("success"):
                logger.info(f"🔥 Viral repost: '{reply_text[:50]}...' — standalone published")
            else:
                logger.warning(f"Viral repost failed: {result.get('error')}")
        except Exception as e:
            logger.error(f"Viral repost error: {e}")

    def _query_reactions(self, reply_id: str) -> list:
        """
        Запрашивает реакции через Nostr адаптер.
        Возвращает список реакций.
        """
        if not self.adapter:
            return []
        try:
            # Ищем kind:7 (likes) и kind:1 (replies) с тегом [e] = reply_id
            events = self.adapter.fetch_recent_posts(since_sec_ago=7200, limit=50)
            
            reactions = []
            for event in events:
                event_obj = event.get("event", event)
                kind = event_obj.get("kind", 1)
                tags = event_obj.get("tags", [])
                pubkey = event_obj.get("pubkey", "")
                
                # Проверяем, ссылается ли событие на наш reply
                for tag in tags:
                    if len(tag) >= 2 and tag[0] == "e" and tag[1] == reply_id:
                        reactions.append({
                            "id": event_obj.get("id", ""),
                            "kind": kind,
                            "pubkey": pubkey,
                            "content": event_obj.get("content", ""),
                        })
                        break
            
            return reactions
        except Exception as e:
            logger.debug(f"Reaction query error: {e}")
            return []

    def _is_reaction_new(self, event_id: str, kind: int, pubkey: str) -> bool:
        """Проверяет, не обрабатывали ли уже это событие."""
        conn = None
        try:
            import sqlite3
            conn = sqlite3.connect(self.mem.db)
            row = conn.execute(
                "SELECT 1 FROM remora_reactions WHERE event_id=? LIMIT 1",
                (event_id,)
            ).fetchone()
            return row is None
        except:
            return True
        finally:
            if conn:
                conn.close()

    @staticmethod
    def _age_hours(created_at_str: str) -> float:
        """Вычисляет возраст треда в часах."""
        try:
            from datetime import datetime
            created = datetime.strptime(created_at_str, "%Y-%m-%d %H:%M:%S")
            delta = datetime.utcnow() - created
            return delta.total_seconds() / 3600
        except:
            return 0

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
