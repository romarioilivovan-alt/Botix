# Резюме: Fix #0 выполнен, готов Fix Pack #1-14

## ✅ Что уже сделано (Fix #0 - Security)

1. **config.json удалён из git** - файл остался локально, но больше не отслеживается
2. **config.example.json создан** - шаблон с плейсхолдерами `YOUR_WEB_UID_HERE` и т.д.
3. **.gitignore обновлён** - добавлены: `config.json`, `secrets.json`, `*.session`, `data.sqlite`, `*.log`
4. **web_uid validation** - в `backend/config.py` проверка на плейсхолдеры при загрузке
5. **401/403 kill_switch** - в `backend/mexc_trader.py` отслеживание auth failures:
   - После 3 подряд 401/403 → `kill_switch=True`
   - Автоматическое закрытие всех позиций через market orders
   - Блокировка новых сделок
6. **Git history очищена** - `git filter-repo` выполнен, `config.json` стёрт из всей истории
7. **Force-push сделан** - удалённый репозиторий обновлён

## 📋 Что делать дальше

### Шаг 1: Запустить Claude Code с инструкциями

В терминале в папке проекта выполни:

```bash
# Убедись что working tree чистый
git status

# Если есть uncommitted changes - закоммить или stash
```

Затем дай Claude Code команду:

```
Прочитай BOTIX_FIXES_BRIEF.md в корне репозитория и выполни описанный там план полностью. 
Работай по коммитам в указанном порядке. Перед стартом убедись что git working tree чистый 
(если нет — спроси меня что делать). После каждого коммита покажи мне его краткое summary 
и переходи к следующему.
```

### Шаг 2: Что будет сделано (14 коммитов)

**Критичные для edge:**
1. **Bug A** - Stock fair contamination (самый важный!)
2. **Bug J+K** - Stricter raw_momentum filters
3. **Bug L** - Universe force_include fix
4. **Fair-cross exit** - Hysteresis для выхода

**Критичные для latency:**
5. **Bug B+C** - Entry latency=0 + split fast/slow loop
6. **Bug D** - query_order для fill detection
7. **Bug F+G** - Welford O(1) σ + cache compute_stats
8. **Bug H+I** - WS watchdog + heartbeat

**Инфраструктура:**
9. **Bug E** - Reuse SymbolStats (меньше вызовов)
10. **Per-symbol buckets** - Разные entry_z для разных классов активов
11. **Signal decision logging** - SQLite таблица для анализа rejections
12. **Latency probe** - Измерение end-to-end latency
13. **Rust skeleton** - Подготовка к миграции hot-path
14. **README update** - Документация изменений

### Шаг 3: После выполнения всех коммитов

Claude Code создаст PR. **НЕ МЁРЖЬ СРАЗУ!**

1. **Logger-режим на 2 часа:**
   ```bash
   git checkout fix/edge-and-latency-pack-1
   python main.py --mode paper --log-level DEBUG
   ```
   
   Проверь логи на:
   - Нет тихих exceptions
   - `signal_decisions` таблица пишется
   - `latency_probe` показывает разумные числа:
     - `binance_depth_age_ms` < 30ms
     - `stats_compute_ms` < 1ms
     - `mexc_depth_age_ms` < 50ms

2. **Paper-режим на 24 часа:**
   - Запусти paper trading на новой ветке
   - Дождись PF > 1.3 на 24-часовом окне
   - **Старая статистика STATUS_REPORT больше неактуальна** - edge нужно подтверждать заново

3. **Только после успешного paper → мёрж в main**

4. **Real-режим** - только после подтверждения edge в paper

## ⚠️ Важные замечания

### Про Fix #1 (Bug A) - САМЫЙ КРИТИЧНЫЙ
Сейчас стоки без Binance reference **загрязняют** `spread_samples` мусорными значениями.
Это приводит к:
- Заниженной σ
- Завышенным z-score
- Ложным сигналам на стоках

После фикса стоки будут:
- Возвращать `fair=None`, `external_fair_available=False`
- Использовать только `bb_revert` стратегию (на MEXC bid-ask spread)
- Не влиять на σ расчёты для крипты

### Про Fix #3 (Bug C) - Split loop
Разделение на fast/slow loop критично для latency:
- **Fast loop (50ms):** reconcile_quotes, reconcile_positions, kill_switch
- **Slow loop (1s):** balance refresh, equity logging, housekeeping

Это снизит latency SL/TP updates с ~200ms до ~50ms.

### Про Fix #6 (Bug F) - Welford
Переход с O(n) на O(1) для σ расчёта:
- Сейчас: каждый `compute_stats` пересчитывает mean/std по всем samples
- После: инкрементальный update, O(1) query

**РИСК:** Если ошибка в реализации Welford → все σ-based стратегии сломаются.
Поэтому обязательно проверить в paper-режиме.

### Про Rust skeleton (Fix #13)
Это **только подготовка**, реальный код будет позже:
- Фаза 1: WS консьюмеры MEXC+Binance
- Фаза 2: Aggregator + stats
- Фаза 3: Execution

Пока просто создаются файлы структуры проекта.

## 🎯 Ожидаемые результаты

После применения всех фиксов:

**Edge improvement:**
- Hit rate должен вырасти на 5-10% за счёт фильтрации ложных сигналов
- Win rate на стоках должен стабилизироваться (сейчас они дают много ложняков)

**Latency improvement:**
- Entry latency: 200ms → 0ms (taker mode)
- Stats compute: ~5ms → <1ms (Welford + cache)
- SL/TP update: ~200ms → ~50ms (fast loop)
- Fill detection: ~500ms → ~100ms (query_order)

**Observability:**
- Таблица `signal_decisions` покажет где утекает edge
- Таблица `latency_probe` покажет узкие места в pipeline

## 📊 Как анализировать результаты

После 24 часов paper trading:

```bash
# Топ-10 причин отказа в сигналах
python scripts/analyze_rejections.py

# Latency breakdown
sqlite3 data.sqlite "SELECT 
  AVG(binance_depth_age_ms) as avg_bn_age,
  AVG(mexc_depth_age_ms) as avg_mx_age,
  AVG(stats_compute_ms) as avg_stats,
  AVG(submit_latency_ms) as avg_submit,
  AVG(fill_latency_ms) as avg_fill
FROM latency_probe 
WHERE ts > strftime('%s', 'now', '-24 hours')"

# Сравнение hit rate до/после
# (нужно будет сравнить с baseline до фиксов)
```

## 🚨 Если что-то пошло не так

### Откат конкретного фикса
Каждый фикс = отдельный коммит, можно откатить точечно:

```bash
# Найти проблемный коммит
git log --oneline

# Откатить конкретный коммит (не последний)
git revert <commit-hash>

# Или откатить последний
git reset --hard HEAD~1
```

### Полный откат всего Pack #1
```bash
git checkout main
git branch -D fix/edge-and-latency-pack-1
```

### Если paper показывает регрессию
1. Проверь логи на exceptions
2. Проверь `signal_decisions` - какие reasons доминируют
3. Проверь `latency_probe` - нет ли аномальных значений
4. Сравни σ до/после (может быть ошибка в Welford)

## 📞 Контрольные точки

**Перед мёржем в main:**
- [ ] Logger-режим 2 часа без exceptions
- [ ] Paper-режим 24 часа с PF > 1.3
- [ ] `signal_decisions` таблица заполняется корректно
- [ ] `latency_probe` показывает разумные числа
- [ ] Нет регрессии в hit rate vs baseline

**Перед переходом в real:**
- [ ] Paper PF > 1.3 на 24h окне
- [ ] Win rate стабилен
- [ ] Max drawdown в пределах нормы
- [ ] Нет аномалий в latency

---

**Статус:** Fix #0 ✅ выполнен, готов к применению Fix Pack #1-14

**Следующий шаг:** Запустить Claude Code с командой из раздела "Шаг 1" выше
