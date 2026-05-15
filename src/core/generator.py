"""
Remora — Generator.
3 режима генерации ответов: short / medium / deep.
+ контент-политика (блокировка нежелательного).
"""
import json
import logging
import sys
import os
from datetime import datetime
import random

logger = logging.getLogger("remora.generator")

from src.core.llm_client import call_llm

# Заблокированные паттерны (контент-политика)
BLOCKED_PATTERNS = [
    "купи", "продай", "buy now", "sell", "financial advice",
    "not financial advice", "nigger", "faggot", "porn", "nsfw",
    "I'm not a financial advisor",
]

TONES = [
    "playful", "deadpan", "absurdist", "wholesome",
    "analytical", "cynical", "mystical", "blunt",
]

SHORT_PROMPT = """You are Remora, a witty Nostr commentator.
Reply to this post with a short, sharp observation.
Keep it under 120 chars. Be smart, not mean.

Tone: {tone}
Original post: "{text}"
Your reply:"""

MEDIUM_PROMPT = """You are Remora, a crypto analyst on Nostr.
Write a thoughtful reply (3-5 sentences). Include context or data.
Do NOT give financial advice. Do NOT say "buy" or "sell".

Tone: {tone}
Original post: "{text}"
Bitcoin price: ~${btc_price}, 24h: {btc_change}%
Your reply:"""

DEEP_PROMPT = """You are Remora, a Nostr prophet.
Make a specific falsifiable prediction with a date (within 7 days).
Explain your reasoning in 2-3 sentences.
Do NOT give financial advice. No "buy"/"sell".

Tone: {tone}  
Original post: "{text}"
Your prediction:"""

CONTINUE_PROMPT = """You are Remora, a witty Nostr commentator.
Someone replied to YOUR comment. Continue the conversation naturally.

Your previous comment: "{remora_reply}"
Their reply: "{user_reply}"
Original post context: "{original_post}"

Write a short, sharp response to their reply (1-2 sentences, max 150 chars).
Be consistent with your previous tone. Don't repeat yourself.
Do NOT give financial advice.

Tone: {tone}
Your reply:"""


class Generator:
    """Генератор ответов Remora. 3 режима + контент-политика."""

    def __init__(self, memory=None, config: dict = None):
        self.mem = memory
        self.config = config or {}
        self.short_ratio = self.config.get("short_ratio", 0.7)
        self.medium_ratio = self.config.get("medium_ratio", 0.2)
        self.deep_ratio = self.config.get("deep_ratio", 0.1)

    def generate(self, candidate: dict, mode: str = None, is_chain: bool = False) -> dict:
        """
        Генерирует ответ на пост.
        mode: auto | short | medium | deep
        is_chain: True для цепочек диалогов (используется continue_conversation)
        Возвращает: {text, mode, tone, blocked}
        """
        text = candidate.get("post", candidate).get("text", str(candidate))
        if isinstance(candidate, dict) and "post" in candidate:
            text = candidate["post"].get("text", "")

        # Для цепочек — используем continue_conversation
        if is_chain:
            tone = candidate.get("tone", "playful")
            context = candidate.get("context", "")
            reply_text = self.continue_conversation(
                original_post=context,
                remora_reply=context,
                user_reply=text,
                tone=tone
            ).get("text", "")
            is_blocked, reason = self._check_policy(reply_text)
            return {
                "text": reply_text,
                "mode": candidate.get("mode", "short"),
                "tone": tone,
                "blocked": is_blocked,
                "reason": reason if is_blocked else "",
            }

        # Выбор режима
        if mode is None or mode == "auto":
            mode = self._choose_mode(candidate)

        # Выбор тона
        tone = self._choose_tone()
        if self.mem:
            self.mem.record_tone_use(tone)

        # Генерация
        reply_text = self._generate_reply(text, mode, tone)
        
        # Контент-политика
        is_blocked, reason = self._check_policy(reply_text)
        if is_blocked:
            logger.warning(f"⛔ Blocked reply: {reason}")
            # Повторная генерация с другим тоном
            for alt_tone in TONES:
                if alt_tone == tone:
                    continue
                reply_text = self._generate_reply(text, mode, alt_tone)
                is_blocked, _ = self._check_policy(reply_text)
                if not is_blocked:
                    tone = alt_tone
                    break
            if is_blocked:
                reply_text = "[blocked by content policy]"
        
        return {
            "text": reply_text,
            "mode": mode,
            "tone": tone,
            "blocked": is_blocked,
            "reason": reason if is_blocked else "",
        }

    def _choose_mode(self, candidate: dict) -> str:
        """Выбирает режим ответа на основе оценки."""
        score = candidate.get("score", "B")
        if score == "A":
            # A: 30% short, 50% medium, 20% deep
            r = random.random()
            if r < 0.3:
                return "short"
            elif r < 0.8:
                return "medium"
            else:
                return "deep"
        else:
            # B: 80% short, 20% medium
            r = random.random()
            return "short" if r < 0.8 else "medium"

    def _choose_tone(self) -> str:
        """Выбирает тон. Приоритет: Telegram preference (preferred_tone.txt) → memory best → random."""
        try:
            _tone_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "preferred_tone.txt")
            if os.path.exists(_tone_file):
                with open(_tone_file) as _f:
                    _pref = _f.read().strip()
                if _pref in TONES:
                    return _pref
        except:
            pass
        if self.mem:
            return self.mem.get_best_tone()
        return random.choice(TONES)

    def continue_conversation(self, original_post: str = "", remora_reply: str = "",
                               user_reply: str = "", tone: str = "playful") -> dict:
        """
        Генерирует продолжение диалога, когда кто-то ответил на ответ Remora.
        Возвращает {text, mode, tone, blocked}
        """
        if self.mem:
            self.mem.record_tone_use(tone)
        
        prompt = CONTINUE_PROMPT.format(
            original_post=original_post[:300],
            remora_reply=remora_reply[:300],
            user_reply=user_reply[:300],
            tone=tone,
        )
        
        result = call_llm(
            system_prompt="",
            user_prompt=prompt,
            model="",
            max_tokens=150,
            temperature=0.7,
            timeout=20,
        )
        
        if not result:
            logger.warning("Empty LLM response for chain dialog, using fallback")
            result = self._fallback(tone)
        
        result = result.strip().strip('"').strip("'")
        
        # Контент-политика
        is_blocked, reason = self._check_policy(result)
        if is_blocked:
            logger.warning(f"Blocked chain reply: {reason}")
            for alt_tone in TONES:
                if alt_tone == tone:
                    continue
                alt_prompt = CONTINUE_PROMPT.format(
                    original_post=original_post[:300],
                    remora_reply=remora_reply[:300],
                    user_reply=user_reply[:300],
                    tone=alt_tone,
                )
                alt_result = call_llm("", alt_prompt, "", 150, 0.7, 20)
                if alt_result:
                    alt_result = alt_result.strip().strip('"').strip("'")
                    alt_blocked, _ = self._check_policy(alt_result)
                    if not alt_blocked:
                        result = alt_result
                        tone = alt_tone
                        is_blocked = False
                        break
            if is_blocked:
                result = "[blocked by content policy]"
        
        return {
            "text": result,
            "mode": "chain",
            "tone": tone,
            "blocked": is_blocked,
            "reason": reason if is_blocked else "",
        }

    def _generate_reply(self, original_text: str, mode: str, tone: str) -> str:
        """Генерирует reply через LLM."""
        # Простейшая конъюнктура BTC (можно заменить на реальный API)
        btc_price = 82000
        btc_change = "+1.2"
        
        if mode == "short":
            prompt = SHORT_PROMPT.format(text=original_text[:200], tone=tone)
            max_tokens = 80
            model = ""
        elif mode == "medium":
            prompt = MEDIUM_PROMPT.format(
                text=original_text[:250], tone=tone,
                btc_price=btc_price, btc_change=btc_change
            )
            max_tokens = 200
            model = ""
        else:  # deep
            prompt = DEEP_PROMPT.format(
                text=original_text[:250], tone=tone,
                btc_price=btc_price, btc_change=btc_change
            )
            max_tokens = 300
            model = ""
        
        result = call_llm(
            system_prompt="",
            user_prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=0.7,
            timeout=20,
        )
        
        if not result:
            logger.warning("Empty LLM response, using fallback")
            return self._fallback(tone)
        
        # Очистка
        result = result.strip().strip('"').strip("'")
        return result

    def _check_policy(self, text: str) -> tuple:
        """Проверяет текст на контент-политику. Возвращает (blocked, reason)."""
        if not text:
            return True, "empty response"
        text_lower = text.lower()
        for pattern in BLOCKED_PATTERNS:
            if pattern in text_lower:
                return True, f"blocked pattern: '{pattern}'"
        return False, ""

    @staticmethod
    def _fallback(tone: str = "playful") -> str:
        """Fallback-ответы при отказе LLM."""
        fallbacks = {
            "playful": "Interesting take. Though I'd argue the data tells a slightly different story. 🧐",
            "deadpan": "The data disagrees with this assessment.",
            "absurdist": "In an alternate timeline, this take would make perfect sense. This is not that timeline.",
            "wholesome": "Love seeing people engage with these ideas. Keep thinking! 🙏",
            "analytical": "Let's look at the numbers: on-chain volume suggests the opposite trend.",
            "cynical": "Markets have a way of humbling takes like this one.",
            "mystical": "The blockchain whispers truths that charts cannot capture.",
            "blunt": "Nope. Check the data.",
        }
        return fallbacks.get(tone, fallbacks["playful"])
