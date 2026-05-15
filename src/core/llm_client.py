"""
Remora — LLM client.
Mistral (primary) с ротацией ключей при ошибках.
"""

import logging
import requests

logger = logging.getLogger("remora.llm")

MISTRAL_KEYS = [
    "GOyjqiGSe3dgxSORNvMLiSeN7f7LRN1L",
    "UslErbFfkYxU8iX3pIhRaMX0G0vgh3mk",
]
MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"


def _mistral_request(messages: list, model: str, max_tokens: int,
                     temperature: float, timeout: int) -> str:
    """Пробует ключи Mistral по очереди при ошибках."""
    for key in MISTRAL_KEYS:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        try:
            resp = requests.post(MISTRAL_URL, headers=headers, json=payload, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"].strip()
            elif resp.status_code == 429:
                logger.warning(f"Mistral 429 (rate limit) with key: {key[:12]}...")
                continue
            elif resp.status_code == 402:
                logger.warning(f"Mistral key exhausted (402), trying next")
                continue
            else:
                logger.warning(f"Mistral {resp.status_code}: {resp.text[:80]}")
                return ""
        except requests.Timeout:
            logger.warning(f"Mistral timeout with key: {key[:12]}...")
            continue
        except Exception as e:
            logger.warning(f"Mistral error: {e}")
            continue
    return ""


def call_llm(
    system_prompt: str,
    user_prompt: str,
    model: str = "",
    max_tokens: int = 50,
    temperature: float = 0.5,
    timeout: int = 25,
) -> str:
    """Вызов Mistral с ротацией ключей."""
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    mistral_model = model if model else "mistral-small-latest"
    return _mistral_request(messages, mistral_model, max_tokens, temperature, timeout)


def call_llm_cheap(
    user_prompt: str,
    system_prompt: str = "",
    max_tokens: int = 5,
    temperature: float = 0.1,
) -> str:
    """Быстрый дешёвый вызов для классификации."""
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})
    return _mistral_request(messages, "mistral-tiny-latest", max_tokens, temperature, 15)
