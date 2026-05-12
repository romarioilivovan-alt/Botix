# 🚀 Готово к запуску на VPS

## Что сделано

✅ **Конфигурация обновлена для live теста:**
- Mode: `real` (live trading)
- Symbols: BCH_USDT, PEPE_USDT, TAO_USDT, ENA_USDT, UNI_USDT (5 монет)
- Max positions: 2 (консервативно)
- Leverage: 50x (снижен со 100x)
- Account share: 30% (используется только 30% баланса)
- Daily loss kill: 10% (бот остановится при 10% дневной потере)
- Max drawdown kill: 20% (бот остановится при 20% общей просадке)

✅ **Документация создана:**
- `VPS_DEPLOYMENT.md` - полная инструкция по деплою
- Команды мониторинга
- Процедуры rollback
- Критерии успеха/провала

✅ **Все изменения запушены в ветку `fix/edge-and-latency-pack-1`**

## Следующие шаги на VPS

### 1. Подключиться к VPS
```bash
ssh user@your-vps-ip
cd /path/to/botix
```

### 2. Загрузить новую ветку
```bash
# Backup текущего config
cp config.json config.json.backup

# Загрузить новую ветку
git fetch origin
git checkout fix/edge-and-latency-pack-1
git pull origin fix/edge-and-latency-pack-1
```

### 3. Скопировать config.json с локальной машины
```bash
# С твоего компьютера:
scp C:\Users\romar\OneDrive\Desktop\VOLODYA\mexc0feesflipper_friendfix1\config.json user@vps-ip:/path/to/botix/config.json
```

### 4. Проверить config
```bash
python3 -c "from backend.config import load_config; cfg = load_config(); print(f'Mode: {cfg.mode}, Symbols: {cfg.universe.include_only}')"
```

Должно вывести:
```
Mode: real, Symbols: ['BCH_USDT', 'PEPE_USDT', 'TAO_USDT', 'ENA_USDT', 'UNI_USDT']
```

### 5. Запустить в tmux
```bash
tmux new -s botix
python3 main.py

# Отключиться: Ctrl+B, затем D
# Вернуться: tmux attach -t botix
```

## Мониторинг (первые 30 минут критичны!)

### Проверить что всё работает:
```bash
# Вернуться к tmux сессии
tmux attach -t botix

# Должны видеть:
# ✅ "Connected to MEXC WebSocket"
# ✅ "Connected to Binance WebSocket"
# ✅ "Universe loaded: 5 symbols"
# ✅ "depth update" логи
```

### Статистика сделок (через 1-2 часа):
```bash
sqlite3 data.sqlite "SELECT COUNT(*) as total, SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) as wins, SUM(pnl_usdt) as total_pnl FROM trades WHERE mode='real'"
```

### Последние сделки:
```bash
sqlite3 data.sqlite "SELECT datetime(ts, 'unixepoch', 'localtime'), symbol, side, pnl_usdt, close_reason FROM trades WHERE mode='real' ORDER BY id DESC LIMIT 10"
```

### Анализ rejections:
```bash
python3 scripts/analyze_rejections.py 1
```

## Критерии остановки (КРАСНЫЕ ФЛАГИ)

**Немедленно останови бота если:**
- ❌ Win rate < 30% после 10+ сделок
- ❌ Общий PnL < -20 USDT
- ❌ Частые ошибки 401/403 (auth failed)
- ❌ WebSocket постоянно переподключается
- ❌ Latency > 50ms стабильно

**Остановка:**
```bash
# В tmux: Ctrl+C
# Или:
pkill -SIGINT -f "python main.py"
```

## Ожидаемые результаты (2-4 часа)

### Хороший сценарий:
- 5-15 сделок
- Win rate: 50-60%
- Общий PnL: +5 до +20 USDT
- Latency: 2-5ms

### Приемлемый сценарий:
- 3-10 сделок
- Win rate: 40-50%
- Общий PnL: -2 до +10 USDT
- Latency: 5-10ms

## Важные файлы на VPS

- `config.json` - конфигурация (НЕ в git)
- `data.sqlite` - база данных со сделками
- `botix.log` - логи (если запущен с nohup)
- `VPS_DEPLOYMENT.md` - полная инструкция

## Контакты

Если что-то пошло не так:
1. Останови бота (Ctrl+C)
2. Сохрани логи и БД
3. Свяжись со мной

---

**Текущее время**: 2026-05-11 23:52 UTC  
**Статус**: ✅ Готово к деплою  
**Риск**: Консервативный (30% баланса, 2 позиции макс, kill switches на 10%/20%)

**Удачи! 🚀**
