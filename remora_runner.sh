#!/bin/bash
# Remora — автостартовый скрипт с watchdog.
# Запускает Remora, перезапускает при падении с backoff.
# Защита от плодящихся демонов: PID-файл, count guard, backoff.
#
# Поведение:
# - 1-й крах: ждёт 3 сек → перезапуск
# - 2-й крах подряд: ждёт 15 сек → перезапуск
# - 3+ крах подряд: ждёт 60 сек → перезапуск
# - Если PID-файл уже есть и процесс жив — не запускает дубль
# - После 5 крахов за час — стоп (веерная блокировка)

cd /home/agent/data/projects/remora || exit 1

PID_FILE="remora.pid"
LOG_FILE="logs/remora.log"
BACKOFF_FILE="/tmp/remora_backoff"
BACKOFF_MAX=5
BACKOFF_WINDOW=3600  # 1 час

# Счётчик крахов (за час)
_crash_count() {
    if [ -f "$BACKOFF_FILE" ]; then
        local now
        now=$(date +%s)
        local cutoff=$((now - $BACKOFF_WINDOW))
        # Читаем файл, считаем крахи за последний час
        local count=0
        while IFS=: read -r ts; do
            [ "$ts" -gt "$cutoff" ] 2>/dev/null && count=$((count + 1))
        done < "$BACKOFF_FILE"
        echo "$count"
        return 0
    fi
    echo 0
}

# Регистрация краха
_record_crash() {
    echo "$(date +%s)" >> "$BACKOFF_FILE"
    # Чистим старые записи (старше часа)
    local now
    now=$(date +%s)
    local cutoff=$((now - $BACKOFF_WINDOW))
    local tmp_file="${BACKOFF_FILE}.tmp"
    while IFS=: read -r ts; do
        [ "$ts" -gt "$cutoff" ] 2>/dev/null && echo "$ts" >> "$tmp_file"
    done < "$BACKOFF_FILE"
    mv "$tmp_file" "$BACKOFF_FILE" 2>/dev/null
}

# Форсированный убиватор: убиваем ВСЕ процессы remora (если пошёл разнос)
_kill_all_remora() {
    local my_pid=$$
    for pid in $(pgrep -f "src/core/remora.py" 2>/dev/null); do
        [ "$pid" = "$my_pid" ] && continue
        [ "$pid" = "$(cat "$PID_FILE" 2>/dev/null)" ] && continue
        echo "[WATCHDOG] Killing duplicate remora PID $pid"
        kill -15 "$pid" 2>/dev/null
        sleep 0.5
        kill -9 "$pid" 2>/dev/null
    done
    # Чистим PID-файл если наш PID не жив
    if [ -f "$PID_FILE" ]; then
        local stored
        stored=$(cat "$PID_FILE" 2>/dev/null)
        if ! kill -0 "$stored" 2>/dev/null; then
            rm -f "$PID_FILE"
            echo "[WATCHDOG] Stale PID cleaned"
        fi
    fi
}

echo "=== Remora Watchdog ==="
echo "Старт: $(date -u)"
echo "PID: $$"

# Anti-spawn guard: убиваем дубликаты скрипта
for pid in $(pgrep -f "remora_runner.sh" 2>/dev/null); do
    [ "$pid" = "$$" ] && continue
    echo "[WATCHDOG] Killing duplicate watchdog PID $pid"
    kill -9 "$pid" 2>/dev/null
done

# Анти-дубль через PID-файл (встроен в Python код)
# Просто запускаем

CRASH_COUNT=0
CONSECUTIVE=0

while true; do
    # Форс-килл дубликатов на всякий случай
    _kill_all_remora
    
    # Определяем задержку перед запуском (backoff)
    local_crashes=$(_crash_count)
    if [ "$local_crashes" -ge "$BACKOFF_MAX" ]; then
        echo "[WATCHDOG] ⛔ $local_crashes крахов за час. Блокировка на 1800 сек."
        echo "Remora упала $local_crashes раз за час. Блокирую запуск на 30 мин." >> "$LOG_FILE"
        sleep 1800
        # Сброс счётчика после блокировки
        rm -f "$BACKOFF_FILE"
        continue
    fi
    
    if [ "$CONSECUTIVE" -eq 0 ]; then
        DELAY=0
    elif [ "$CONSECUTIVE" -eq 1 ]; then
        DELAY=3
    elif [ "$CONSECUTIVE" -eq 2 ]; then
        DELAY=15
    else
        DELAY=60
    fi
    
    [ "$DELAY" -gt 0 ] && echo "[WATCHDOG] Backoff ${DELAY}сек (crash #$CONSECUTIVE)" && sleep "$DELAY"
    
    echo "[WATCHDOG] Запуск Remora..."
    cd /home/agent/data/projects/remora
    
    # Запускаем в foreground — watchdog ждёт его завершения
    python3 -u src/core/remora.py >> logs/remora.log 2>&1
    EXIT_CODE=$?
    
    _record_crash
    CONSECUTIVE=$((CONSECUTIVE + 1))
    
    echo "[WATCHDOG] ⚠️ Remora упала (exit code: $EXIT_CODE) — crash #$CONSECUTIVE"
done
