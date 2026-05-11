## Live start

### Что уже готово
- `line A real` настроен и запускается через:
  - `H:\pepe_bots_workspace\bots\mexc0feesflipper_friendfix1\start_real_lineA_contract_v2.bat`
- live-конфиг:
  - `H:\pepe_bots_workspace\bots\mexc0feesflipper_friendfix1\config.real_lineA_contract_v2.json`
- страница:
  - `http://127.0.0.1:8086`

### Что нужно заполнить
В live для первого старта достаточно `MEXC Web UID`.

`device_id` и `mhash` бот достраивает сам.

### Порядок запуска
1. Открыть `http://127.0.0.1:8086`
2. Проверить, что сверху режим `real`
3. Вставить `MEXC Web UID` в правом блоке `Config`
4. Нажать `Save`
5. Подождать 5-10 секунд
6. Обновить страницу
7. Проверить:
   - `Auth: OK`
   - `balance > 0`
8. Только после этого нажать `Start`

### Если auth не поднялся
1. Остановить окно/процесс
2. Снова запустить:
   - `H:\pepe_bots_workspace\bots\mexc0feesflipper_friendfix1\start_real_lineA_contract_v2.bat`
3. Еще раз открыть `http://127.0.0.1:8086`

### Важные замечания
- Не крутить symbols/algorithms руками через UI.
- Для первой live-проверки использовать только `line A`.
- `line B` запускать отдельно, не одновременно с `line A`.
- Live сейчас ближе к paper, чем раньше, но это не гарантия прибыли.
