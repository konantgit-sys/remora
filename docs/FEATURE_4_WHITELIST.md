# Remora #4 — Whitelist Авторов

**Статус:** ✅ Реализовано

**Дата:** 2026-05-10

---

## Задача

Если кто-то хотя бы раз ответил на пост Remora, то **всегда отвечаем** этому автору, даже если его пост получил score=C (низкое качество).

**Почему:** Engagement > перфекционизм. Если человек уже взаимодействует, стоит поддержать диалог.

---

## Архитектура

### Memory методы

```python
add_to_whitelist(pubkey: str, reason: str = "replied_to_remora")
```
- Добавляет автора в whitelist (устанавливает score=1.0)
- Логирует добавление

```python
is_whitelisted(pubkey: str) -> bool
```
- Проверяет если score >= 0.9

```python
get_whitelisted_authors() -> list
```
- Возвращает всех whitelisted авторов (для статистики)

### Evaluator изменения

В `_prefilter_score()` добавлена проверка:

```python
# 0.5. Whitelist check
if self.mem and self.mem.is_whitelisted(pubkey):
    return 99  # Гарантированный приоритет
```

Score=99 гарантирует что пост попадёт в LLM и будет оценен, даже если он очень короткий или без ключевых слов.

### Tracker интеграция

В `_fetch_reactions()` при обнаружении `kind:1` (reply):

```python
# Whitelist: если кто-то ответил → добавляем в whitelist
if r["kind"] == 1:
    self.mem.add_to_whitelist(r["pubkey"], reason="replied_to_remora")
```

Это гарантирует что первый же ответ от человека добавит его в whitelist.

---

## Поток данных

1. **Человек отвечает на пост Remora** → Event kind:1 с e-тегом на пост Remora
2. **Tracker сканирует** → Находит реакцию через `_fetch_reactions()`
3. **Whitelist trigger** → `add_to_whitelist(author_pubkey)`
4. **Следующий пост от этого автора**:
   - Pre-filter видит `is_whitelisted() = True`
   - Возвращает score=99
   - Пост попадает в LLM
   - LLM оценивает (может быть B, C, даже D если спам)
   - Если не D → ответ будет опубликован

---

## Практический результат

### До whitelist:
- Автор: A → Remora: ✅ Ответила
- Человек: Отвечает на ответ Remora
- Автор: B (низкое качество) → Remora: ❌ Пропущено (score=C)
-人: Но я хотел диалог! 😞

### После whitelist:
- Автор: A → Remora: ✅ Ответила
- Человек: Отвечает на ответ Remora → Добавлен в whitelist
- Автор: B (низкое качество) → Remora: ✅ **Ответила** (так как whitelisted)
- 人: Диалог продолжился! 😊

---

## Ограничения

- Спам все ещё блокируется на уровне `_is_hard_spam()` (хард-триггеры)
- Score=D (явный спам) не переходит в ответ даже для whitelisted
- Whitelist это о **приоритете**, а не об **отсутствии фильтров**

---

## Метрика успеха

После недели:
- Должно быть 5-10 whitelisted авторов
- Они должны получать ответы на каждый пост (даже низкого качества)
- Engagement rate с ними должен расти

---

## Связь с другими фичами

- **#1 Chain-диалоги** — сам себя добавляет в whitelist когда ответит на reply
- **#2 Self-learning** — whitelisted авторы помогают собирать данные о лучших тонах
- **#3 Viral upgrade** — если whitelisted автор получает много реакций → viral boost

