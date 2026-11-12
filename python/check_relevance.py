"""
Regression check for the topic filters (needs OPENAI_API_KEY).

Runs real headlines from past posts through the same filters the bot uses:
English items through translate_article (expect __SKIP__ or a post),
Russian items through is_on_topic (expect False or True). Exits non-zero
if any verdict differs, so run it after editing either prompt:

  python check_relevance.py
"""

import asyncio
import sys

from translator import is_on_topic, translate_article

# (title, description, expected_on_topic)
EN_CASES = [
    ("‘Rap music is on the move’: A hazy studio session with 1300SAINT",
     "We hang out with the rapper and his collective in the studio as they finish their new record.", False),
    ("Black Country, New Road are blending classical training with indie rock",
     "The band talk about their new album, touring and how they write songs together.", False),
    ("OpenAI VP: How AI Changed My Family’s Fight Against Cancer | Ashley Alexander",
     "A personal story about using ChatGPT to navigate a cancer diagnosis and treatment options.", False),
    ("Balenciaga names Martine Rose creative director as Piccioli exits",
     "Kering-owned Balenciaga has appointed British designer Martine Rose as creative director, effective January 2027.", True),
    ("Justin Boxford Exits The Estée Lauder Cos. to Pursue New Opportunity",
     "The executive, who led the company’s North America business, is leaving after 12 years.", True),
    ("Levi’s claymation holiday campaign celebrates handmade craft amid AI bloat",
     "The denim brand’s holiday spot, created with agency FCB, uses stop-motion animation.", True),
]

RU_CASES = [
    ("Страховая пенсия в 2026 году: кому положена и как рассчитать её размер самостоятельно",
     "Рассказываем, как устроена страховая пенсия, какие нужны баллы и стаж.", False),
    ("Для людей с диабетом разработали специальную футбольную программу",
     "Британские врачи запустили программу тренировок для пациентов с диабетом второго типа.", False),
    ("Бренд украшений Viva La Vika объявил о закрытии",
     "Российский бренд украшений Viva La Vika сообщил о прекращении работы после 10 лет на рынке.", True),
    ("VK добавила возможность продвижения в каналах Max через VK AdBlogger",
     "Рекламодатели смогут размещать интеграции у авторов каналов в мессенджере Max.", True),
]


async def main() -> int:
    failures = 0
    for title, desc, expected in EN_CASES:
        text = await translate_article(title, desc)
        got = not text.strip().startswith("__SKIP__")
        ok = got == expected
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'} EN expected={'post' if expected else 'skip'} "
              f"got={'post' if got else 'skip'}: {title}")
    for title, desc, expected in RU_CASES:
        got = await is_on_topic(title, desc)
        ok = got == expected
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'} RU expected={'yes' if expected else 'no'} "
              f"got={'yes' if got else 'no'}: {title}")
    total = len(EN_CASES) + len(RU_CASES)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
