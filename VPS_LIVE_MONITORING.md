# VPS Live Test - Мониторинг

**Дата запуска:** 2026-05-12 01:11 UTC
**Сервер:** 3.112.231.49 (AWS Tokyo)
**Процесс:** python.exe (PID 7668)
**Баланс:** 56.03 USDT

## Конфигурация

- **Mode:** real (live trading)
- **Монеты:** BCH_USDT, PEPE_USDT, TAO_USDT, ENA_USDT, UNI_USDT (5 штук)
- **Стратегия:** raw_momentum
- **Max позиций:** 2
- **Leverage:** 50x
- **Account share:** 30% (используется ~16.8 USDT)
- **Entry latency:** 0ms
- **Autostart:** true

## Риск-менеджмент

- **Daily loss kill:** 10% (~5.6 USDT)
- **Max drawdown kill:** 20% (~11.2 USDT)
- **Min trade margin:** 2 USDT
- **Max notional:** 10,000 USDT

## Команды для мониторинга

### Подключиться к серверу
```powershell
mstsc /v:3.112.231.49
# User: Administrator
# Pass: GhbSoh(G61WrW9oKSmUzrdUEF6)l$9HC
```

### Проверить статус бота
```powershell
cd C:\Users\Administrator\Desktop\Boter\Botix

# Проверить процесс
Get-Process python | Select-Object Id, StartTime, CPU, WorkingSet

# Проверить логи (последние 50 строк)
Get-Content bot_output.log -Tail 50

# Проверить сделки
python -c "import sqlite3; conn = sqlite3.connect('data.sqlite'); cur = conn.cursor(); cur.execute('SELECT COUNT(*) FROM trades WHERE mode=\"real\"'); print(f'Total trades: {cur.fetchone()[0]}'); conn.close()"
```

### Проверить статистику
```powershell
cd C:\Users\Administrator\Desktop\Boter\Botix

# Создать скрипт проверки
@"
import sqlite3
from datetime import datetime
conn = sqlite3.connect('data.sqlite')
cur = conn.cursor()

# Общая статистика
cur.execute('SELECT COUNT(*), SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END), SUM(pnl_usdt) FROM trades WHERE mode="real"')
row = cur.fetchone()
print(f'Total trades: {row[0]}')
print(f'Wins: {row[1]}')
print(f'Total PnL: {row[2]:.2f} USDT')

# Последние 10 сделок
cur.execute('SELECT datetime(ts, "unixepoch", "localtime"), symbol, side, pnl_usdt, close_reason FROM trades WHERE mode="real" ORDER BY id DESC LIMIT 10')
print('\nLast 10 trades:')
for row in cur.fetchall():
    print(f'{row[0]} | {row[1]} | {row[2]} | PnL: {row[3]:.2f} | {row[4]}')

# Статистика по монетам
cur.execute('SELECT symbol, COUNT(*), SUM(pnl_usdt) FROM trades WHERE mode="real" GROUP BY symbol ORDER BY SUM(pnl_usdt) DESC')
print('\nPnL by symbol:')
for row in cur.fetchall():
    print(f'{row[0]}: {row[1]} trades, {row[2]:.2f} USDT')

conn.close()
"@ | Out-File -Encoding UTF8 stats.py

python stats.py
```

### Остановить бота
```powershell
# Найти процесс
Get-Process python

# Остановить (замени PID на актуальный)
Stop-Process -Id 7668 -Force
```

### Перезапустить бота
```powershell
cd C:\Users\Administrator\Desktop\Boter\Botix

# Остановить старый
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force

# Запустить новый
Start-Process python -ArgumentList "start_bot.py" -WorkingDirectory "C:\Users\Administrator\Desktop\Boter\Botix" -WindowStyle Hidden

# Проверить логи через 10 секунд
Start-Sleep -Seconds 10
Get-Content bot_output.log -Tail 30
```

## Что смотреть

### Первые 30 минут (критично!)
- ✅ Бот подключился к MEXC и Binance WebSocket
- ✅ Появляются кандидаты (candidates_log)
- ⏳ Появляются сделки (trades)
- ⏳ PnL положительный или близкий к нулю

### Первые 2-4 часа
- Ожидается: 5-15 сделок
- Win rate: 40-60%
- Общий PnL: -2 до +20 USDT
- Latency: 2-10ms

### Красные флаги (остановить бота!)
- ❌ Drawdown > 10% за час
- ❌ 5+ убыточных сделок подряд
- ❌ PnL < -5 USDT за первый час
- ❌ Нет кандидатов 10+ минут
- ❌ WebSocket disconnected

## Файлы на сервере

- `C:\Users\Administrator\Desktop\Boter\Botix\` - папка проекта
- `config.json` - конфигурация (НЕ коммитить!)
- `data.sqlite` - база данных со сделками
- `bot_output.log` - логи бота
- `start_bot.py` - скрипт запуска

## Веб-интерфейс

URL: http://3.112.231.49:8080

Здесь можно:
- Посмотреть текущие позиции
- Увидеть статистику
- Остановить/запустить торговлю

## Контакты

Если что-то пошло не так:
1. Остановить бота: `Stop-Process -Id 7668 -Force`
2. Проверить логи: `Get-Content bot_output.log -Tail 100`
3. Проверить PnL: запустить `stats.py`
4. Написать мне результаты

---

**Статус:** 🟢 Бот запущен, ждём первых сделок
**Следующая проверка:** через 30 минут (01:45 UTC)
