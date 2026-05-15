"""
Remora — Scanner.
Сканирует релеи Nostr через RemoraAdapter, фильтрует посты.
Полностью самостоятельный.
"""
import json
import logging
import time

logger = logging.getLogger("remora.scanner")

from src.core.remora_adapter import RemoraAdapter, RELAYS, SCAN_RELAYS

# Ключевые слова для фильтрации крипто/техно/философии
KEYWORDS = [
    "bitcoin", "btc", "crypto", "nostr", "defi", "ethereum", "eth",
    "blockchain", "sats", "lightning", "halving", "mining", "whale",
    "market", "price", "support", "resistance", "bull", "bear",
    "consensus", "proof", "protocol", "decentralize", "sovereign",
    "freedom", "liberty", "token", "swap", "liquidity", "yield",
    "philosophy", "consciousness", "reality", "agent", "autonomy",
]

MAX_POSTS_PER_SCAN = 30
SCAN_AGE_MINUTES = 120
SCAN_RELAY_COUNT = 10  # V2: 10 релеев (было 15) — экономия ~25%


class Scanner:
    """Сканер ленты Nostr. Отбирает посты, достойные ответа."""

    def __init__(self, adapter: RemoraAdapter, memory=None, config: dict = None):
        self.adapter = adapter
        self.mem = memory
        self.config = config or {}
        self._pubkey_hex = adapter.get_pubkey_hex()
        self._scan_relays = SCAN_RELAYS[:SCAN_RELAY_COUNT]
        logger.info(f"🔍 Scanner initialized, {len(self._scan_relays)} relays (economy mode: {SCAN_RELAY_COUNT})")

    def scan(self) -> list:
        """
        Сканирует релеи, возвращает список постов для оценки.
        Каждый пост: {id, pubkey, text, created_at, relay_url}
        """
        posts = self.adapter.fetch_recent_posts(
            since_sec_ago=SCAN_AGE_MINUTES * 60,
            limit=20,
            relays=self._scan_relays,
        )

        # Фильтр 1: не отвечать себе
        posts = [p for p in posts if p["pubkey"] != self._pubkey_hex]

        # Фильтр 2: проверка по памяти (дубликаты + blacklist)
        if self.mem:
            filtered = []
            for p in posts:
                if self.mem.is_duplicate(p["id"]):
                    continue
                if self.mem.is_blacklisted(p["pubkey"]):
                    continue
                filtered.append(p)
            posts = filtered

        # Фильтр 3: лимит
        posts = posts[:MAX_POSTS_PER_SCAN]

        if posts:
            logger.info(f"📡 Scanned: {len(posts)} candidates")
        return posts

    def get_relay_count(self) -> int:
        return len(self._scan_relays)
