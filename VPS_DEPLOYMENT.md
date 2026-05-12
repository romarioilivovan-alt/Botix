# VPS Live Test Deployment Instructions

**Date**: 2026-05-12  
**Mode**: REAL (live trading)  
**Branch**: fix/edge-and-latency-pack-1  
**Test Duration**: 2-4 hours initial observation

## Configuration Summary

### Trading Parameters
- **Symbols**: BCH_USDT, PEPE_USDT, TAO_USDT, ENA_USDT, UNI_USDT (5 coins)
- **Max concurrent positions**: 2
- **Leverage**: 50x
- **Account share**: 30% (only 30% of balance will be used)
- **Margin per slot**: 15%
- **Min trade margin**: 2 USDT

### Risk Limits (CONSERVATIVE)
- **Daily loss kill switch**: 10% (bot stops if daily loss exceeds 10%)
- **Max drawdown kill switch**: 20% (bot stops if total drawdown exceeds 20%)
- **Max notional per trade**: 10,000 USDT

### Strategy
- **Algorithm**: raw_momentum (multi-timeframe momentum with lag filters)
- **Entry Z**: 1.8σ
- **Signal max age**: 400ms
- **Require 5s agreement**: true
- **Require lag**: true (min 1.5 bps)
- **Max chase**: 3.0 bps

## Deployment Steps

### 1. На VPS: Подготовка

```bash
# Подключиться к VPS
ssh user@your-vps-ip

# Перейти в директорию проекта
cd /path/to/botix

# Остановить текущий бот (если запущен)
pkill -f "python main.py" || true

# Сделать backup текущей конфигурации
cp config.json config.json.backup.$(date +%Y%m%d_%H%M%S)

# Переключиться на новую ветку
git fetch origin
git checkout fix/edge-and-latency-pack-1
git pull origin fix/edge-and-latency-pack-1
```

### 2. Скопировать config.json на VPS

**С локальной машины:**
```bash
# Скопировать обновлённый config.json на VPS
scp config.json user@your-vps-ip:/path/to/botix/config.json
```

**Или вручную отредактировать на VPS:**
```bash
nano config.json
```

Убедиться что:
- `"mode": "real"`
- `"include_only"` содержит 5 монет
- `"max_concurrent_positions": 2`
- `"account_share_pct": 0.3`
- `"daily_loss_pct_kill": 0.10`

### 3. Проверка конфигурации

```bash
# Проверить что config загружается
python3 -c "from backend.config import load_config; cfg = load_config(); print(f'Mode: {cfg.mode}, Symbols: {len(cfg.universe.include_only)}')"

# Должно вывести: Mode: real, Symbols: 5
```

### 4. Запуск бота

**Вариант A: В tmux (рекомендуется)**
```bash
# Создать новую tmux сессию
tmux new -s botix

# Запустить бота
python3 main.py

# Отключиться от tmux (бот продолжит работать): Ctrl+B, затем D
# Вернуться к сессии: tmux attach -t botix
```

**Вариант B: В screen**
```bash
screen -S botix
python3 main.py
# Отключиться: Ctrl+A, затем D
# Вернуться: screen -r botix
```

**Вариант C: В фоне с логами**
```bash
nohup python3 main.py > botix.log 2>&1 &
tail -f botix.log
```

## Мониторинг

### Логи в реальном времени
```bash
# Если в tmux/screen - просто смотреть вывод
# Если в фоне:
tail -f botix.log
```

### Проверка позиций
```bash
# Открытые позиции через API (если есть веб-интерфейс)
curl http://localhost:8080/api/positions

# Или через SQLite
sqlite3 data.sqlite "SELECT symbol, side, entry, qty, pnl_usdt FROM trades WHERE close_ts IS NULL"
```

### Статистика сделок
```bash
# Последние 20 сделок
sqlite3 data.sqlite "SELECT datetime(ts, 'unixepoch', 'localtime') as time, symbol, side, pnl_usdt, close_reason FROM trades ORDER BY id DESC LIMIT 20"

# Общая статистика
sqlite3 data.sqlite "SELECT COUNT(*) as total, SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) as wins, SUM(pnl_usdt) as total_pnl FROM trades WHERE mode='real'"
```

### Анализ rejections
```bash
python3 scripts/analyze_rejections.py 1  # последний час
```

### Latency probe
```bash
sqlite3 data.sqlite "SELECT symbol, AVG(decision_ms), AVG(submit_latency_ms), AVG(fill_latency_ms) FROM latency_probe WHERE ts > strftime('%s', 'now', '-1 hour') GROUP BY symbol"
```

## Критические моменты для наблюдения

### Первые 30 минут
- ✅ Бот успешно подключился к MEXC и Binance WebSocket
- ✅ Universe загрузился (5 символов)
- ✅ Нет ошибок аутентификации (401/403)
- ✅ Книги обновляются (логи "depth update")
- ✅ Статистика вычисляется (логи "compute_stats")

### Первая сделка
- ✅ Сигнал прошёл все фильтры
- ✅ Ордер успешно размещён
- ✅ Fill детектирован быстро (< 100ms)
- ✅ Позиция отслеживается
- ✅ SL/TP работают

### Первый час
- ✅ Win rate > 40%
- ✅ Средний PnL > -0.5 USDT
- ✅ Нет зависаний WebSocket
- ✅ Latency < 10ms на hot path

## Остановка бота

### Graceful shutdown
```bash
# Если в tmux/screen - просто Ctrl+C
# Если в фоне:
pkill -SIGINT -f "python main.py"
```

### Emergency stop
```bash
pkill -9 -f "python main.py"
```

## Rollback (если что-то пошло не так)

```bash
# Остановить бота
pkill -f "python main.py"

# Вернуться на main
git checkout main

# Восстановить старый config
cp config.json.backup.YYYYMMDD_HHMMSS config.json

# Перезапустить
python3 main.py
```

## Ожидаемые результаты (первые 2-4 часа)

### Оптимистичный сценарий
- 5-15 сделок
- Win rate: 50-60%
- Средний PnL: +0.5 до +2 USDT
- Общий PnL: +5 до +20 USDT
- Latency: 2-5ms decision time

### Реалистичный сценарий
- 3-10 сделок
- Win rate: 40-50%
- Средний PnL: -0.2 до +1 USDT
- Общий PnL: -2 до +10 USDT
- Latency: 5-10ms decision time

### Красные флаги (немедленно остановить)
- ❌ Win rate < 30% после 10+ сделок
- ❌ Средний PnL < -2 USDT
- ❌ Общий PnL < -20 USDT
- ❌ Частые ошибки аутентификации
- ❌ WebSocket постоянно переподключается
- ❌ Latency > 50ms стабильно

## Контакты для экстренной связи

Если что-то пошло не так:
1. Остановить бота (Ctrl+C или pkill)
2. Сохранить логи: `cp botix.log botix.error.$(date +%Y%m%d_%H%M%S).log`
3. Сохранить БД: `cp data.sqlite data.error.$(date +%Y%m%d_%H%M%S).sqlite`
4. Связаться со мной с логами

## Checklist перед запуском

- [ ] Backup текущей конфигурации сделан
- [ ] Новая ветка fix/edge-and-latency-pack-1 загружена
- [ ] config.json обновлён (mode=real, 5 монет, консервативный риск)
- [ ] Config валидируется без ошибок
- [ ] Баланс на MEXC достаточен (минимум 50-100 USDT рекомендуется)
- [ ] tmux/screen сессия создана
- [ ] Готов мониторить первые 30 минут

---

**ВАЖНО**: Это live тестирование с реальными деньгами. Первые 2-4 часа требуют активного мониторинга. Не оставляй бота без присмотра в первый день.
