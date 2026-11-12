"""
Translates English news articles to natural Russian using OpenAI gpt-4o-mini,
and checks Russian articles for relevance to the channel topic.
"""

import logging

from openai import AsyncOpenAI

import config

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None

# Channel scope. Shared by the translator prompt (English feeds) and the
# yes/no relevance check (Russian feeds), so both filter by the same rules.
TOPIC_SCOPE = (
    "beauty и косметика, уход, парфюмерия; fashion, luxury, streetwear, украшения и аксессуары; "
    "ритейл и e-commerce; маркетинг, реклама, PR, брендинг, медиа и adtech; "
    "SMM, соцсети, инфлюенсеры и creator economy"
)

ON_TOPIC_HINT = (
    "Новость подходит, если она о бренде или компании из этих сфер (запуск, закрытие, "
    "сделка, финансовые результаты, коллекция, рекламная кампания), о кадровом назначении "
    "в индустрии или о маркетинговом, PR- или ритейл-кейсе."
)

OFF_TOPIC_EXAMPLES = (
    "музыка, релизы и интервью музыкантов; кино, сериалы, шоу; жизнь и интервью "
    "знаменитостей без бренда, коллекции или кампании; общие новости технологий и AI "
    "без связи с маркетингом, рекламой или ритейлом; медицина, здоровье, наука; политика; "
    "личные финансы, пенсии, налоги, ЖКХ, транспорт; спорт без бренда или кампании"
)

SYSTEM_PROMPT = """Ты редактор Telegram-канала о beauty и fashion индустрии, маркетинге, PR и influencer-стратегиях. Тебе дают заголовок и описание новости на английском.

ВАЖНО — ОТКАЗ ОТ ПУБЛИКАЦИИ.
Если выполняется хотя бы одно из условий ниже — верни РОВНО строку `__SKIP__` (без кавычек, без пояснений, без пустых строк, без HTML) и больше ничего не пиши:
1. Описание — общая платитюда без конкретных имён дизайнеров/CEO/брендов, названий коллекций/продуктов, цифр. Примеры: «Бренды запускают новые коллекции», «Мода становится более устойчивой», «Эксперты считают, что важно следить за трендами», «Тридцать шесть лет спустя дизайнер возвращается к корням».
2. В описании слеплены 3+ разных, не связанных одной темой новости в один абзац.
3. Тема новости — войны, армии, оборона, ВПК, спецоперации, мобилизация, теракты, оккупированные территории, ситуации с правами человека в зонах конфликтов, нападения на журналистов в зонах боевых действий, гуманитарные катастрофы.
4. Тема новости — порно, OnlyFans, секс-скандалы, утечки интимных фото, секс-работа.
5. Тема новости не относится к тематике канала (""" + TOPIC_SCOPE + """). Например: """ + OFF_TOPIC_EXAMPLES + """. """ + ON_TOPIC_HINT + """

Если новость подходит — напиши пост на живом, естественном русском — 2–3 предложения. Без буквальных переводов. Без вводных слов «Итак», «Сегодня», «Стоит отметить». Без заголовка. Один пост — одна новость, не объединяй несколько тем.

Форматируй текст в Telegram HTML:
- <b>жирным</b> — ключевые слова и главная суть (1–3 слова на предложение)
- <i>курсивом</i> — имена собственные, названия компаний/продуктов, все числа и цифры
- <u>подчёркнутым</u> — только если есть чёткий вывод или итог, не более 4 слов в конце поста
- <tg-spoiler>скрытым</tg-spoiler> — только если контент пугающий, шокирующий или неприличный
- Разумно добавляй эмодзи (1–3 штуки по смыслу, не в каждом предложении)
- Если несколько абзацев — разделяй пустой строкой
- Спецсимволы &, <, > вне тегов — экранируй как &amp; &lt; &gt;

Выводи только готовый текст поста, без пояснений. Или `__SKIP__` если новость не подходит."""


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    return _client


async def translate_article(title: str, description: str) -> str:
    """
    Translate and rewrite article to natural Russian.
    Falls back to the raw title on error.
    """
    user_message = f"Заголовок: {title}\nОписание: {description}"
    try:
        resp = await _get_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            max_tokens=300,
            temperature=0.4,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error("OpenAI translation failed: %s", e)
        return title


RELEVANCE_PROMPT = (
    "Ты фильтр новостей для Telegram-канала. Тематика канала: " + TOPIC_SCOPE + ". "
    + ON_TOPIC_HINT + " Новость НЕ подходит, если это: " + OFF_TOPIC_EXAMPLES + ". "
    "Тебе дают заголовок и описание новости. Ответь одним словом: ДА или НЕТ."
)


async def is_on_topic(title: str, description: str) -> bool:
    """
    Return False only when the model clearly says the article is off-topic.
    Fails open (True) on API errors or unexpected answers, so an OpenAI
    outage keeps Russian feeds posting the way they did before this check.
    """
    user_message = f"Заголовок: {title}\nОписание: {description}"
    try:
        resp = await _get_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": RELEVANCE_PROMPT},
                {"role": "user", "content": user_message},
            ],
            max_tokens=3,
            temperature=0,
        )
        answer = (resp.choices[0].message.content or "").strip().upper()
    except Exception as e:
        logger.error("OpenAI relevance check failed: %s", e)
        return True
    if answer.startswith("НЕТ"):
        return False
    if not answer.startswith("ДА"):
        logger.warning("Unexpected relevance answer %r for: %s", answer, title)
    return True
