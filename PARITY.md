# Чек-лист паритета GUI ↔ TUI

Отметка состояния переноса второстепенных виджетов TUI в GUI
(план, задача 7). Образцы — `drcode/src/drcode/tui/…`; источники данных —
только `drcode.core` / `usage_store` / `stats` / `settings`, обращений к
`tui/` из GUI-кода нет (в тестах тоже — семантика воспроизводится, а не
импортируется).

## 1. Todo-панель чек-листа — ✅ перенесено

Образец: `tui/todo_panel.py` + `refresh_todo_panel` / `action_toggle_todo_panel`
(`tui/sidebar_view.py`).

- [x] Полоса чек-листа между историей чата и полем ввода —
  `src/drcodedesktop/todo_panel.py`, смонтирована в `ChatTimelineView`.
- [x] Только чтение: список принадлежит агенту (todo_create / todo_update).
- [x] Счётчик `todos N/N` в шапке; клик по шапке сворачивает/разворачивает.
- [x] Автосворачивание полностью выполненного списка; ручной override
  живет в рамках одной задачи и сбрасывается при переключении.
- [x] Скрыта, когда задачи нет или список пуст.
- [x] Обновление по `task_updated` (дебаунс rebuild) и при открытии задачи.

## 2. Сервис-события в чате — ✅ перенесено (задача 3 + задача 7)

Образец: роли `event`/`system` в `tui/chat_timeline.py` +
`drcode/service_events.py`.

- [x] События задачи (`task.events`) идут в единый таймлайн с
  таймстампами — `chat_timeline_model.build_timeline` (порт
  `_build_task_timeline`).
- [x] Рендер `event ›` красным в чат-панели (`chat_view._line_block`).
- [x] Живой путь: `service_event` хаба → дебаунс-rebuild панели;
  запись идёт через `CoreShell._show_event` (продакшн-шов пайплайна).
- [x] Тесты: `tests/test_parity_widgets.py::test_service_events_render_in_chat`.

## 3. Activity-индикатор + stall watchdog — ✅ перенесено

Образец: `tui/activity_view.py` (354 строки).

- [x] Индикатор снизу панели очереди — `src/drcodedesktop/activity_view.py`,
  тик 1 с (`poll_once`).
- [x] Состояния: `○ idle`; `▶ working` (fresh, <30 c) с текущим тулом и
  возрастом; `⚠ stalled` (stale) с обратным отсчётом авто-наддача;
  `⏳ network retry` / `⏳ network outage` (ожидание отложенного ретрая,
  `drcode.core.usage.NETWORK_RETRIES`); `⚠ agent: idle` — задача ACTIVE,
  но воркера нет; компактная сводка при нескольких параллельных ранах
  (количество + возраст самого молчаливого).
- [x] Stall watchdog в шелле — `CoreShell._poll_stall` (тик 1 с): ран без
  признаков жизни дольше `settings.stall_nudge.minutes` абортится и
  перезапускается через настоящий `_nudge_run` (только этот ран; параллельные
  слоты не трогаются; агент — continuation-ход, тестер — новый раунд
  через `_advance`). Отсчёт в индикаторе соответствует реальному поведению.
- [x] Статистика сессии (развернутый хедер TUI): `tasks done`, avg/median
  времени активация→комплит, `bugs:features`, точное токен-потребление
  сессии (`SessionTokenUsage`) — `_collect_session_stats` в шелле + строка
  индикатора.
- [x] Git-индикатор `⎇ N` (незмердженные ветки завершенных задач) —
  `CoreShell._unmerged_completed_ids` через wholesale-снимок
  `task_env.drcode_branch_state()` (блокирующий git — в потоке), опрос
  раз в 10 с.

### Явные отличия/пропуски в activity-блоке

- [ ] **Аудит hook-merge** (`_hook_merge_still_owns`, бэк-стамп
  `hook_merge_failed`, `_purge_stale_merge_reports`) — НЕ перенесено:
  относится к merge-экрану (задача 9). Незмердженность считается по
  голому снимку веток, без вычета hook-автоматизированных слотов.
- [ ] **Deferred-install рераны** (`_install_rerun_candidates`,
  `_maybe_rerun_pending_installs`) — НЕ перенесено (hook-домен, задача 9).
- [ ] Сетевые счетчики хедера (`net retries: N total / M active`,
  probe round-trip, `probe fails`) — частично: ретраи видны в строках
  `⏳ network retry/outage`; полная строка соединения хедера TUI не
  переносилась (в GUI нет model-status-хедера — модель и провайдер
  показываются в тулбаре).
- [ ] GitHub-строка хедера (`GitHub: on | last sync …`) — НЕ перенесена
  (синхронизация GitHub в GUI не настраивается и не polled'ится; это
  отдельный экран TUI-настроек вне задачи 7).

## 4. Usage / статистика — ✅ перенесено

Образцы: `tui/usage_dialog.py`, `tui/stats_dialog.py`; данные:
`drcode/usage_store.py`, `drcode/stats.py`.

- [x] «Reports → Token usage…» — `usage_dialog.py`: таблица
  день × модель-ключ (`<role>/<model>`), Requests/Input/Output/Cache
  read/Cache write, тысячи-разделители, строка `Σ period`, строка-статус
  с периодом и числом ключей, подсказка про пустой архив. Чтение файла —
  вне event loop (`asyncio.to_thread`), диалог read-only.
- [x] Явный `UsageStore(path=usage_file())` — дисциплина инцидента
  ebe6ba15 (без дефолтного конструктора под `DRCODE_TASK_ID`).
- [x] «Reports → Statistics…» (Ctrl+G) — `stats_dialog.py`: таблица
  14/30 UTC-дней (создано/выполнено/отменено/провалено + средние
  разработка/тесты/резолв через `format_seconds`), две спарклайна
  (создано/выполнено, Qt-вариант — столбиковый `Sparkline`), клавиша
  `p` переключает окно 14↔30, каждый показ — свежий потоковый снапшот
  (throwaway `StatsStore`, только чтение).

### Явные отличия

- [x] Спарклайны — столбиковая отрисовка QPainter вместо текстового
  `Sparkline` Textual (форма — близкая, это осознанная замена виджета,
  данные те же).
- [ ] Табы диалога статистики TUI (один таб «Проект») схлопнуты в
  плоский диалог — в TUI табовая обвязка уже сейчас одноэлементная.

## 5. Уведомления — ✅ перенесено

Образец: `tui/notifications_dialog.py` + `drcode/notifications.py`.

- [x] Диалог «Settings → Notifications…» — `notifications_dialog.py`:
  чекбокс Enabled, поле ntfy.sh topic, живая строка статуса («включено,
  но топика нет» / «события пойдут в ntfy.sh/<topic>»), сеть не
  пробуется.
- [x] Save → `CoreShell.apply_notifications_settings`: пишет
  `notifications_enabled` + `ntfy_topic` в `<project>/.drcode/settings.json`
  (тот же файл, что пишет TUI), эмитит confirm-notify и сразу шлет
  тестовый push через живой `Notifier` (settings-getter — изменения
  подхватываются без пересборки).
- [x] Cancel/ESC не пишет ничего (семантика TUI).
- [x] Ошибки доставки нотификаций всплывают как notify-события
  (`_notify_error` — было с задачи 1).

## 6. Сайдбар задач — осознанное расхождение с TUI (переработка колонок)

Образец: `tui/sidebar_view.py` (строки задач); данные — те же
(`TaskManager` + снапшот `drcode_branch_state`), но колонки GUI — свои:

- [x] Заголовок группы без TUI-минуса (`QUEUE (3)`, минус/плюс в GUI
  заменяет шеврон дерева) и на всю ширину панели
  (`setFirstColumnSpanned`) — имя группы и счётчик не обрезаются.
- [x] Колонки строк: маркеры (✅/❌ верификация, ✖ cancelled, ✋
  needs human, ⏸ unfinished, ⚙ resolver — один столбец), ✓ завершения
  (цвет = mergedness из `CoreShell._merge_state`: зелёный merged /
  жёлтый unmerged / серый «коммита не было»), `B|R|F #id` (автоширина
  по контенту), начало названия; полный титул — в тултипе.
- [x] Отдельных колонок kind / status / flags больше нет: статус виден
  по секции, тип — буква в id-колонке.

## 5a. Настройки самого шелла + системные уведомления — GUI-only добавка

Аналога в TUI нет: это собственные настройки GUI (не drcode) и свой
канал доставки. Ntfy-поведение шелла не менялось (per-task
`notifier.task_status` в шелле по-прежнему не подключён), озвучка
(voice_tts_*) не затронута, `drcode/` не тронут.

- [x] Пункт меню «Preferences…» (`QAction.MenuRole.PreferencesRole`,
  на macOS — в меню приложения с Cmd+,) — доступен всегда, даже без
  открытого проекта; `preferences_dialog.py`.
- [x] Шрифт: ОДНА пара «семейство + размер» (gui.json:
  `font_family`/`font_size`, ""/0 = системный fixed-шрифт) для чата
  (`chat_blocks.default_chat_font` + `apply_font` у блоков и
  `ChatTimelineView`) и списка задач (`TaskQueueView.apply_font`);
  применяется к открытым вью сразу (OK/Apply), без переоткрытия проекта.
- [x] Уведомления: 4 независимых от voice_tts_* чекбокса (gui.json
  `notify_completed` / `notify_tester_failed` / `notify_clarification` /
  `notify_pipeline`), по умолчанию выключены.
- [x] Доставка — `tray_notifications.py`: `QSystemTrayIcon.showMessage`;
  иконка видна, пока открыт проект И включён хотя бы один тип.
- [x] Источник событий: GUI-собственный слушатель статусов менеджера
  (только origin == "system"; завершение / провал тестера — TESTING→QUEUED
  и FAILED-вердикт / уточнение) + settled-состояния (пусто/заблокировано)
  через `TrayEventBridge(Notifier)` — piggyback на существующие вызовы
  `notifier.pipeline_idle`/`pipeline_busy` пайплайна, ntfy-путь не тронут;
  prime при старте, дедуп по смене состояния (как `_notifications_primed`).
- [x] Тексты — переиспользованы из ядра: `_status_text` и фраза
  `queue_empty` из `drcode.voice` (в языке объявлений проекта).
- [x] Уведомление не показывается, когда главное окно активно
  (`isActiveWindow()`).

## Приёмка

- [x] Все источники данных — `drcode.core` / `usage_store` / `stats` /
  `settings` (плюс `task_env.drcode_branch_state` для git-индикатора);
  обращений к `tui/` нет ни в `src/drcodedesktop/`, ни в `tests/`.
- [x] Чек-лист выше заполнен; отсутствующее помечено явными `[ ]`
  пунктами с указанием, куда это относится.
- [x] Зелёный `uv run pytest -n 4`, чистые `ruff` + `pyright`, TUI
  (субмодуль `drcode/`) не тронут.
