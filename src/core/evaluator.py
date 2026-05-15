"""
Remora — Evaluator V3.
Двухэтапная оценка: pre-filter (без LLM) → батч LLM для топ-10.
Вместо 10 вызовов к Mistral — 1 батч-запрос. Экономия ~3 сек/цикл.
D — только за явный спам/мат/вред.
Убраны эвристики NIP-05 и followers из pre-filter (невалидны для Nostr).
"""
import json
import logging
import sys
import os

logger = logging.getLogger("remora.evaluator")

from src.core.llm_client import call_llm_cheap, call_llm


SYSTEM_PROMPT = """You are Remora's evaluator — a binary classifier for Nostr content.
Your job: decide if each post deserves a reply from a crypto-analysis bot.

Evaluate based on:
- Is it about crypto, Nostr, Bitcoin, DeFi, philosophy, tech, or markets?
- Is the author expressing an opinion worth engaging with?
- Is it high quality (not spam, not off-topic)?

For EACH post, respond with EXACTLY ONE letter:
A — high quality, deserves a deep thoughtful reply
B — okay topic, short reply possible  
C — low quality or off-topic, skip entirely
D — EXPLICIT spam, harmful content, hate speech, scams, or aggressive trolling ONLY.
    Do NOT use D for: short posts, no NIP-05, new accounts, low followers, or low effort.
    Those are C (skip silently). D is for genuinely harmful content."""

BATCH_PROMPT = """Below are {count} Nostr posts. For each one, respond with EXACTLY ONE letter (A/B/C/D).

Return ONLY {count} letters separated by commas: e.g. "B,C,A,D,C,B,C,C,C,D"
No explanations. No numbering. Just the comma-separated letters.

{posts}"""


# Ключевые слова для pre-filter (без LLM)
CRYPTO_KEYWORDS = [
    "bitcoin", "btc", "crypto", "nostr", "defi", "ethereum", "eth",
    "blockchain", "sats", "lightning", "halving", "mining", "whale",
    "market", "price", "support", "resistance", "bull", "bear",
    "consensus", "proof", "protocol", "decentralize", "sovereign",
    "freedom", "liberty", "token", "swap", "liquidity", "yield",
    "philosophy", "consciousness", "reality", "agent", "autonomy",
    "fed", "cbdc", "inflation", "money", "bank", "economy",
    "ai", "artificial", "intelligence", "singularity",
]


class Evaluator:
    """Оценщик постов: pre-filter → батч LLM для топ-10. D — только за вред."""

    def __init__(self, memory=None, config: dict = None):
        self.mem = memory
        self.config = config or {}
        self.min_score = self.config.get("min_score", "B")
        self.llm_top_n = 10  # V2: больше постов идут в LLM
        # Слова-триггеры для реальной блокировки (мат, спам, скам)
        self._hard_block_triggers = [
            "nigger", "faggot", "kill yourself", "buy my", "porn",
            "nsfw", "subscribe", "click here", "free money",
        ]

    def _is_hard_spam(self, text: str) -> bool:
        """Проверка на реальный спам/мат — без LLM."""
        lower = text.lower()
        for trigger in self._hard_block_triggers:
            if trigger in lower:
                return True
        return False

    def _prefilter_score(self, post: dict) -> int:
        """
        V2: Упрощённая эвристическая оценка.
        NIP-05 и followers убраны — невалидны для Nostr.
        Фокус на: релевантность темы, длина, вопросы.
        """
        text = (post.get("text") or "").lower()
        pubkey = post.get("pubkey", "")
        score = 0

        # 0. Хард-спам проверка → сразу низкий балл
        if self._is_hard_spam(text):
            return 0

        # 0.5. Whitelist check — максимальный приоритет
        if self.mem and self.mem.is_whitelisted(pubkey):
            return 99

        # 1. Ключевые слова → тема релевантна
        keyword_hits = sum(1 for kw in CRYPTO_KEYWORDS if kw in text)
        if keyword_hits >= 1:
            score += 1
        if keyword_hits >= 3:
            score += 1

        # 2. Длина
        length = len(text)
        if length >= 50:
            score += 1
        if length >= 200:
            score += 1

        # 3. Вопрос → повод ответить
        if "?" in text:
            score += 1

        return max(0, min(10, score))

    def evaluate(self, posts: list) -> list:
        """
        V3: Pre-filter → батч LLM (1 вызов вместо 10).
        """
        results = []
        
        # Этап 1: pre-filter всех постов
        scored = []
        for post in posts:
            pf_score = self._prefilter_score(post)
            text = (post.get("text") or "").lower()
            
            # Хард-спам → сразу D
            if pf_score == 0 and self._is_hard_spam(text):
                results.append({
                    "post": post,
                    "score": "D",
                    "reason": "Hard spam detected (trigger words)",
                    "prefilter": 0,
                })
                if self.mem:
                    self.mem.add_blacklist(
                        post.get("pubkey", ""),
                        reason="Hard spam trigger"
                    )
                continue
            
            scored.append((pf_score, post))
        
        # Сортируем по убыванию
        scored.sort(key=lambda x: x[0], reverse=True)
        
        # V3: топ-10 в LLM (батч)
        llm_candidates = scored[:self.llm_top_n]
        skip_candidates = scored[self.llm_top_n:]
        
        logger.info(f"📊 Pre-filter: {len(scored)} кандидатов, топ-{self.llm_top_n} в LLM (батч), {len(skip_candidates)} пропущено")
        
        # V3: Батч-оценка топ-кандидатов — ОДИН LLM вызов вместо N
        if llm_candidates:
            try:
                batch_scores = self._evaluate_batch(llm_candidates)
            except Exception as e:
                logger.warning(f"Batch LLM evaluate failed: {e}, fallback to single")
                batch_scores = {i: "C" for i in range(len(llm_candidates))}
            
            for idx, (pf_score, post) in enumerate(llm_candidates):
                score = batch_scores.get(idx, "C")
                reason = f"LLM batch: {score}"
                
                # D без хард-триггера → опускаем до C
                if score == "D":
                    text = (post.get("text") or "").lower()
                    if self._is_hard_spam(text):
                        reason = f"LLM D + hard trigger"
                        if self.mem:
                            self.mem.add_blacklist(
                                post.get("pubkey", ""),
                                reason=f"LLM D + hard trigger"
                            )
                    else:
                        score = "C"
                        reason = f"LLM D downgraded to C (no hard trigger)"
                
                results.append({
                    "post": post,
                    "score": score,
                    "reason": reason,
                    "prefilter": pf_score,
                })
        
        # Пропущенные — все C
        for pf_score, post in skip_candidates:
            results.append({
                "post": post,
                "score": "C",
                "reason": f"Pre-filter: {pf_score}/10 (low priority)",
                "prefilter": pf_score,
            })
        
        # Финальная сортировка A→B→C→D
        results.sort(key=lambda x: {"A": 0, "B": 1, "C": 2, "D": 3}.get(x["score"], 2))
        
        scores = {"A": 0, "B": 0, "C": 0, "D": 0}
        for r in results:
            scores[r["score"]] = scores.get(r["score"], 0) + 1
        logger.info(f"📊 Final: {scores} (LLM calls: 1 batch)")
        
        return results

    def _evaluate_batch(self, candidates: list) -> dict:
        """
        V3: Оценивает топ-кандидатов ОДНИМ батч-запросом к LLM.
        Возвращает словарь {idx: score_letter, ...}
        """
        # Формируем список постов для батча
        post_entries = []
        for idx, (_, post) in enumerate(candidates):
            text = (post.get("text") or "")[:200]
            pubkey = (post.get("pubkey") or "")[:16]
            post_entries.append(f"{idx+1}. \"{text}\"\n   Author: {pubkey}..")
        
        posts_text = "\n\n".join(post_entries)
        count = len(candidates)
        
        user_prompt = BATCH_PROMPT.format(count=count, posts=posts_text)

        try:
            response = call_llm(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_prompt,
                max_tokens=50,
                temperature=0.1,
                timeout=30,
            )
            
            result = (response or "").strip()
            logger.debug(f"Batch LLM response: {result[:100]}")
            
            # Парсим ответ: ожидаем "A,B,C,D,B,..." — N comma-separated letters
            parts = [p.strip().upper() for p in result.replace(",", " ").split()]
            
            batch = {}
            valid_count = 0
            for idx in range(count):
                if idx < len(parts):
                    char = parts[idx]
                    if len(char) >= 1 and char[0] in ("A", "B", "C", "D"):
                        batch[idx] = char[0]
                        valid_count += 1
                    else:
                        batch[idx] = "C"
                else:
                    batch[idx] = "C"
            
            logger.debug(f"Batch parsed: {valid_count}/{count} valid ratings")
            return batch
            
        except Exception as e:
            logger.warning(f"Batch LLM call failed: {e}")
            # Fallback — все C
            return {idx: "C" for idx in range(count)}

    def filter_worthy(self, evaluated: list) -> list:
        """Только A и B (достойны ответа). Whitelist-авторов пропускаем даже на C."""
        result = []
        for r in evaluated:
            if r["score"] in ("A", "B"):
                result.append(r)
            elif r["score"] == "C":
                pubkey = r.get("post", {}).get("pubkey", "")
                if self.mem and self.mem.is_whitelisted(pubkey):
                    r["score"] = "B"
                    r["reason"] = "Whitelist bypass"
                    result.append(r)
        return result[:20]

    def is_short_only(self, evaluated: list) -> list:
        return [r for r in evaluated if r["score"] == "B"]

    def is_deep_worthy(self, evaluated: list) -> list:
        return [r for r in evaluated if r["score"] == "A"]
