# Beauty News

Автоматический Telegram-канал с новостями beauty & fashion индустрии, маркетинга, PR и influencer-стратегий. Работает через GitHub Actions — без сервера.

## Что делает

Два автономных процесса, запускаемых по расписанию:

| Процесс | Расписание (MSK) | Что делает |
|---|---|---|
| **RSS News** | Каждые 20 мин, 07:00–21:00 | Берёт статью из RSS, переводит на русский через GPT-4o-mini, добавляет Wikipedia-ссылки, постит в Telegram с фото |
| **Deep Research** | 13:00 ежедневно | Через NotebookLM делает глубокое исследование beauty/fashion новостей за 24ч по 7 разделам, генерирует аудио-подкаст, отправляет в Telegram |

Тихие часы (21:00–07:00 MSK) — RSS-новости не публикуются, даже если запуск пришёл в это время.

### Кто запускает

С августа 2026 GitHub cron срабатывает 2–4 раза в день вместо положенных и опаздывает на часы,
поэтому основной триггер — [cron-job.org](https://console.cron-job.org): он вызывает
`POST /repos/kirill-morozov-n/beauty-news/actions/workflows/<file>/dispatches` с телом `{"ref":"master"}`
(fine-grained PAT, право Actions: read & write). Cron в workflow-файлах остался запасным:

- `rss-news.yml` — `concurrency` не даёт двум запускам постить одновременно, `POST_INTERVAL` — постить чаще 10 мин.
- `beauty-news.yml` — запуск от cron-job.org (или ручной Run workflow) выполняется всегда, а запасной
  запуск по cron job `guard` (`.github/actions/skip-if-delivered`) пропускает, если подкаст уже вышел
  за последние 20 ч.

### Диагностика

Actions → **Diagnostics** → Run workflow:
- `feeds` — как каждый фид отвечает раннеру GitHub (многие сайты режут IP дата-центров), плюс кандидаты из `extra_feeds`;
- `relevance` — прогон эталонных заголовков через тематические фильтры (`python/check_relevance.py`). Запускать после правки промптов.

## Структура проекта

```
Beauty News/
├── .github/workflows/
│   ├── rss-news.yml          # Cron: */20 4-18 UTC — одна новость за запуск
│   └── beauty-news.yml       # Cron: 0 3 UTC — deep research + аудио-подкаст
│
├── python/
│   ├── main.py               # CLI: run | schedule | news | news-once | start | login
│   ├── config.py             # Все настройки: промпт исследования, расписание, тихие часы
│   ├── rss_bot.py            # RSS-бот: цикл публикации новостей с дедупликацией
│   ├── rss_feeds.py          # Список RSS-фидов по категориям (beauty/fashion/маркетинг)
│   ├── rss_fetcher.py        # Парсинг RSS, извлечение картинок
│   ├── translator.py         # Перевод статей на русский через OpenAI GPT-4o-mini
│   ├── wiki_linker.py        # Автолинковка имён собственных через Wikipedia API
│   ├── researcher.py         # Пайплайн NotebookLM: research → audio → Telegram
│   ├── scheduler.py          # Локальный планировщик (asyncio, для локального запуска)
│   ├── news_log.py           # Лог покрытых тем deep research (дедупликация)
│   ├── telegram_sender.py    # Отправка в Telegram
│   ├── requirements.txt      # Python-зависимости
│   └── output/               # Рантайм: скачанные файлы, кеши (создаётся автоматически)
│       ├── rss_posted.json       # URL/заголовки опубликованных статей (дедупликация)
│       ├── news_log.json         # Лог тем deep research
│       └── wiki_cache.json       # Кеш Wikipedia-запросов (TTL 7 дней)
```

## Разделы Deep Research

1. **Beauty & fashion (мир)** — запуски, кампании, коллаборации
2. **Beauty & fashion Россия** — запуски, кампании, мероприятия
3. **PR-кейсы** — в beauty/fashion, Россия и мир
4. **Marketing tech** — данные, аналитика, таргетинг, GEO для AI
5. **Influencer marketing** — кейсы, статистика, подборки блогеров
6. **Креаторы и фабрики креаторов** — кейсы, результаты
7. **Стратегия** — маркетинговые стратегии beauty/fashion брендов

## Категории RSS-фидов

Источник истины — `python/rss_feeds.json`.

- **Beauty & Fashion индустрия** — WWD, Glossy, FashionUnited, Just Style, Luxury Daily, Dazed, i-D, Hypebeast, Highsnobiety
- **Beauty** — Allure, Refinery29, Beauty Independent, Beauty Inc, Cosmetics Business, Global Cosmetics News, Premium Beauty News
- **Глянец** — Vogue, Elle, Harper's Bazaar, Cosmopolitan, Marie Claire, W, InStyle
- **Россия** — The Blueprint, Buro247, BeautyHack, Beauty Insider, SRSLY, Sostav, AdIndex
- **Маркетинг / PR** — Marketing Brew, Digiday, AdAge, AdWeek, Campaign, PRWeek, PR Daily, Martech, Marketing Dive
- **Influencer marketing / SMM** — Influencer Marketing Hub, Later, Hootsuite, Sprout Social, Social Media Today
- **Креаторы / creator economy** — Passionfruit, Creator Economy, Tubefilter, Creator Handbook, Digital Content Next
- **Retail / e-commerce** — Retail Dive, Modern Retail

## Секреты GitHub Actions

Настроить в Settings → Secrets and variables → Actions:

| Secret | Описание |
|---|---|
| `OPENAI_API_KEY` | Ключ OpenAI для GPT-4o-mini (перевод статей) |
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота |
| `TELEGRAM_CHANNEL_ID` | ID канала (начинается с `-100...`) |
| `NOTEBOOKLM_STORAGE_STATE` | Base64 от `~/.notebooklm/storage_state.json` (Google auth для NotebookLM) |

### Как получить NOTEBOOKLM_STORAGE_STATE

```bash
cd python
pip install 'notebooklm-py[browser]'
playwright install chromium
python main.py login          # откроется браузер — залогинься в Google
base64 < ~/.notebooklm/storage_state.json | pbcopy   # скопирует в буфер (macOS)
# Вставить в GitHub Secrets как NOTEBOOKLM_STORAGE_STATE
```

Сессия протухает — при ошибках авторизации повторить `login` и обновить секрет.

## Локальный запуск

```bash
cd python
pip install -r requirements.txt
playwright install chromium      # только для deep research
```

### Команды

```bash
python main.py login        # Первичная авторизация Google (NotebookLM)
python main.py run          # Deep research + аудио (разовый запуск)
python main.py schedule     # Deep research по расписанию (ежедневно в SCHEDULE_TIME)
python main.py news         # RSS-бот в бесконечном цикле
python main.py news-once    # Одна RSS-новость и выход (для GitHub Actions)
python main.py start        # RSS-бот + daily research одновременно
```

### Переменные окружения

При локальном запуске задать через env:

```bash
export OPENAI_API_KEY="sk-..."
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_CHANNEL_ID="-100..."
```

## Настройки (config.py)

| Параметр | По умолчанию | Описание |
|---|---|---|
| `RESEARCH_PROMPT` | (7 разделов beauty/fashion) | Промпт для deep research |
| `RESEARCH_MODE` | `deep` | `deep` или `fast` |
| `AUDIO_FORMAT` | `DEEP_DIVE` | Формат аудио: DEEP_DIVE, BRIEF, CRITIQUE, DEBATE |
| `AUDIO_LENGTH` | `DEFAULT` | Длина: SHORT, DEFAULT, LONG |
| `AUDIO_LANGUAGE` | `ru` | Язык аудио |
| `GENERATE_VIDEO` | `False` | Генерация видео (отключено — слишком долго) |
| `SCHEDULE_TIME` | `07:00` | Время ежедневного запуска (для локального scheduler) |
| `QUIET_START` | `21:00` | Начало тихих часов (RSS не постится) |
| `QUIET_END` | `07:00` | Конец тихих часов |
| `RESEARCH_TIMEOUT` | 20 мин | Таймаут ожидания deep research |
| `GENERATION_TIMEOUT` | 60 мин | Таймаут генерации аудио/видео |

## Зависимости

```
notebooklm-py[browser]>=0.3.3   # API клиент NotebookLM + Playwright
feedparser>=6.0.11               # Парсинг RSS
openai>=1.30.0                   # GPT-4o-mini для переводов
beautifulsoup4>=4.12.0           # Парсинг HTML (og:image extraction)
httpx>=0.27.0                    # HTTP-клиент (Telegram API, скачивание картинок)
```

## Расписание (UTC → MSK)

```
UTC 03:00  →  MSK 06:00   Deep Research
UTC 04:00  →  MSK 07:00   RSS-новости начинаются (каждые 20 мин)
UTC 18:00  →  MSK 21:00   RSS-новости останавливаются (тихие часы)
```

## Известные особенности

- **GitHub Actions cron не точен** — задержки до 5–15 мин при нагрузке на серверы GitHub
- **NotebookLM auth протухает** — нужно периодически обновлять `NOTEBOOKLM_STORAGE_STATE`
- **Rate limits** — NotebookLM ограничивает deep research; при rate limit бот ждёт и пробует снова, затем переключается на fast research
- **Дедупликация RSS** — по URL + substring match заголовков
