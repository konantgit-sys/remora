"""
Remora — Nostr Adapter V2.
Публикует через relay-snin.v2.site, fanout через relay-v2 на 3445 релеев.
"""
import json, logging, time, threading, asyncio
from typing import Optional, List

logger = logging.getLogger("remora.adapter")

# ── V2: Только relay-v2 на localhost — гарантированно, fanout делает relay-v2 ──
RELAYS = ["ws://localhost:8198"]

# ── Релеи для сканирования (публичные, живые) ──
SCAN_RELAYS = [
    "wss://purplepag.es", "wss://nos.lol", "wss://relay.nostrplebs.com",
    "wss://nostr.mom", "wss://relay.nostr.band", "wss://relay.damus.io",
    "wss://nostr.oxtr.dev", "wss://relay.primal.net", "wss://nostr.wine",
    "wss://offchain.pub", "wss://nostr.bitcoiner.social", "wss://nostr.land",
    "wss://relay.snort.social", "wss://nostr-pub.wellorder.net", "wss://relay.nostr.info",
]


class RemoraAdapter:
    """Nostr адаптер Remora V2. Публикация через relay-snin.v2.site."""

    def __init__(self, nsec: str):
        self._nsec = nsec
        self._keys = None
        self._signer = None
        self._pubkey_hex = None
        self._pubkey_bech32 = None
        self._init_keys()

    def _init_keys(self):
        from nostr_sdk import Keys, NostrSigner
        self._keys = Keys.parse(self._nsec)
        self._signer = NostrSigner.keys(self._keys)
        self._pubkey_hex = self._keys.public_key().to_hex()
        self._pubkey_bech32 = self._keys.public_key().to_bech32()
        logger.info(f"🔑 Remora pubkey: {self._pubkey_bech32[:16]}...")

    def get_pubkey(self) -> str:
        return self._pubkey_bech32

    def get_pubkey_hex(self) -> str:
        return self._pubkey_hex

    def publish(self, content: str, tags: Optional[List] = None) -> Optional[str]:
        return self.publish_event(content, tags or [], kind=1)

    def publish_event(self, content: str, tags: Optional[List] = None,
                      kind: int = 1) -> Optional[str]:
        """Публикует через relay-snin.v2.site. relay-v2 делает fanout на 3445 релеев."""
        from nostr_sdk import EventBuilder, Tag, Kind

        tags = tags or []
        kind_obj = Kind(kind)

        builder = EventBuilder(kind_obj, content)
        if tags:
            tag_objects = []
            for tag in tags:
                if isinstance(tag, list) and len(tag) >= 2:
                    try:
                        tag_objects.append(Tag.parse(tag))
                    except:
                        pass
            if tag_objects:
                builder = builder.tags(tag_objects)

        async def _sign():
            return await builder.sign(self._signer)
        event = asyncio.run(_sign())
        event_id = event.id().to_hex()

        # Публикуем на relay-v2 (local, гарантированно)
        ok = False
        for url in RELAYS:
            try:
                self._publish_to_relay(url, event)
                ok = True
                logger.info(f"✅ Published to {url}")
            except Exception as e:
                logger.error(f"❌ Fail {url}: {e}")

        if not ok:
            # fallback — relay-snin.v2.site через nginx
            for url in ["wss://relay-snin.v2.site"]:
                try:
                    self._publish_to_relay(url, event)
                    ok = True
                    logger.info(f"✅ Fallback: {url}")
                    break
                except:
                    pass

        if not ok:
            # fallback — публичные релеи
            for url in SCAN_RELAYS[:5]:
                try:
                    self._publish_to_relay(url, event)
                    ok = True
                    logger.info(f"✅ Fallback public: {url}")
                    break
                except:
                    pass

        return event_id if ok else None

    def _publish_to_relay(self, url: str, event):
        """Отправляет подписанное событие на один релей."""
        import websocket as ws

        serialized_tags = []
        for t in event.tags().to_vec():
            serialized_tags.append(t.as_vec())

        event_json = json.dumps([
            "EVENT",
            {
                "id": event.id().to_hex(),
                "pubkey": event.author().to_hex(),
                "created_at": event.created_at().as_secs(),
                "kind": event.kind().as_u16(),
                "tags": serialized_tags,
                "content": event.content(),
                "sig": event.signature(),
            }
        ])

        sock = ws.create_connection(url, timeout=8)
        sock.send(event_json)
        sock.settimeout(10)
        try:
            msg = sock.recv()
            data = json.loads(msg)
            if data[0] == "OK" and data[2] == True:
                logger.debug(f"✅ {url[:30]} OK")
            else:
                logger.warning(f"⚠️ {url[:30]} {data}")
        except:
            pass
        finally:
            try:
                sock.close()
            except:
                pass

    # ── Сканирование (публичные релеи) ──

    def fetch_recent_posts(self, since_sec_ago: int = 3600, limit: int = 20,
                           relays: list = None) -> list:
        """Сканирует публичные релеи, возвращает kind:1 посты."""
        import websocket as ws

        if relays is None:
            relays = SCAN_RELAYS

        since_ts = int(time.time()) - since_sec_ago
        sub_id = "remora_scan"
        req = json.dumps([
            "REQ", sub_id,
            {"kinds": [1], "since": since_ts, "limit": limit}
        ])

        all_posts = {}
        lock = threading.Lock()

        def _scan(url: str):
            try:
                sock = ws.create_connection(url, timeout=6)
                sock.send(req)
                sock.settimeout(3)
                start = time.time()
                while time.time() - start < 5:
                    try:
                        msg = sock.recv()
                        data = json.loads(msg)
                        if data[0] == "EVENT" and data[1] == sub_id:
                            ev = data[2]
                            if ev.get("kind") == 1:
                                with lock:
                                    if ev["id"] not in all_posts:
                                        all_posts[ev["id"]] = {
                                            "id": ev["id"],
                                            "pubkey": ev["pubkey"],
                                            "text": (ev.get("content") or "").strip(),
                                            "created_at": ev.get("created_at", 0),
                                            "relay_url": url,
                                            "tags": ev.get("tags", []),
                                        }
                    except:
                        break
                try:
                    sock.send(json.dumps(["CLOSE", sub_id]))
                    sock.close()
                except:
                    pass
            except:
                pass

        threads = []
        for url in relays:
            t = threading.Thread(target=_scan, args=(url,), daemon=True)
            threads.append(t)
            t.start()
            time.sleep(0.1)

        for t in threads:
            t.join(timeout=8)

        posts = sorted(all_posts.values(),
                       key=lambda x: x.get("created_at", 0), reverse=True)
        return posts

    def fetch_replies_to_post(self, post_id: str, since_sec_ago: int = 86400,
                              limit: int = 30, relays: list = None) -> list:
        """Ищет реплаи на конкретный пост."""
        import websocket as ws

        if relays is None:
            relays = SCAN_RELAYS

        since_ts = int(time.time()) - since_sec_ago
        sub_id = "remora_replies"
        req = json.dumps([
            "REQ", sub_id,
            {"kinds": [1], "since": since_ts, "#e": [post_id], "limit": limit}
        ])

        all_replies = {}
        lock = threading.Lock()

        def _scan(url: str):
            try:
                sock = ws.create_connection(url, timeout=6)
                sock.send(req)
                sock.settimeout(3)
                start = time.time()
                while time.time() - start < 5:
                    try:
                        msg = sock.recv()
                        data = json.loads(msg)
                        if data[0] == "EVENT" and data[1] == sub_id:
                            ev = data[2]
                            if ev.get("kind") == 1:
                                with lock:
                                    if ev["id"] not in all_replies:
                                        all_replies[ev["id"]] = {
                                            "id": ev["id"],
                                            "pubkey": ev["pubkey"],
                                            "text": (ev.get("content") or "").strip(),
                                            "created_at": ev.get("created_at", 0),
                                            "relay_url": url,
                                            "tags": ev.get("tags", []),
                                        }
                    except:
                        break
                sock.send(json.dumps(["CLOSE", sub_id]))
                sock.close()
            except:
                pass

        threads = []
        for url in relays:
            t = threading.Thread(target=_scan, args=(url,), daemon=True)
            threads.append(t)
            t.start()
            time.sleep(0.1)

        for t in threads:
            t.join(timeout=8)

        replies = sorted(all_replies.values(),
                        key=lambda x: x.get("created_at", 0), reverse=True)
        return replies
