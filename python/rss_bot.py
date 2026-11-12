"""
RSS News Bot
Fetches articles from RSS feeds, translates via OpenAI gpt-4o-mini,
and posts to the Telegram channel every 10 minutes with a photo.

Usage (via main.py):
  python main.py news
"""

import asyncio
import json
import logging
import random
import re
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

import config
from rss_feeds import RSS_FEEDS
from rss_fetcher import find_article_with_image
from translator import is_on_topic, translate_article
from wiki_linker import add_wiki_links

logger = logging.getLogger(__name__)

POSTED_URLS_FILE = Path(config.OUTPUT_DIR) / "rss_posted.json"
POST_INTERVAL = 10 * 60  # seconds — min gap between posts
TG_API = "https://api.telegram.org/bot{token}/{method}"
# Telegram caption limit is 1024 chars
CAPTION_LIMIT = 1024


# ---------------------------------------------------------------------------
# Deduplication helpers
# ---------------------------------------------------------------------------

def _load_posted_data() -> dict:
    """Load {urls: [...], titles: [...], last_post_time: "ISO"} from disk."""
    if not POSTED_URLS_FILE.exists():
        return {"urls": [], "titles": []}
    try:
        raw = json.loads(POSTED_URLS_FILE.read_text(encoding="utf-8"))
        # Backwards compat: old format was a plain list of URLs
        if isinstance(raw, list):
            return {"urls": raw, "titles": []}
        return raw
    except Exception:
        return {"urls": [], "titles": []}


def load_posted_urls() -> set:
    return set(_load_posted_data()["urls"])


def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation and extra spaces for fuzzy matching."""
    import re
    t = title.lower().strip()
    t = re.sub(r"[^\w\s]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def load_posted_titles() -> set:
    return {_normalize_title(t) for t in _load_posted_data()["titles"] if t}


def save_posted(url: str, title: str = "", update_time: bool = False) -> None:
    POSTED_URLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = _load_posted_data()
    if url not in data["urls"]:
        data["urls"].append(url)
    if title:
        norm = _normalize_title(title)
        if norm not in {_normalize_title(t) for t in data["titles"]}:
            data["titles"].append(title)
    if update_time:
        data["last_post_time"] = datetime.utcnow().isoformat()
    # keep last 3000 to avoid unbounded growth
    data["urls"] = data["urls"][-3000:]
    data["titles"] = data["titles"][-3000:]
    POSTED_URLS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _seconds_since_last_post() -> float | None:
    """Return seconds since the last successful post, or None if unknown."""
    data = _load_posted_data()
    ts = data.get("last_post_time")
    if not ts:
        return None
    try:
        last = datetime.fromisoformat(ts)
        return (datetime.utcnow() - last).total_seconds()
    except Exception:
        return None


def is_title_duplicate(title: str, posted_titles: set) -> bool:
    """Check if a title is too similar to an already posted one."""
    norm = _normalize_title(title)
    if not norm or len(norm) < 10:
        return False
    # Exact normalized match
    if norm in posted_titles:
        return True
    # Substring match (for titles ≥ 30 chars)
    for prev in posted_titles:
        if len(prev) >= 30 and (prev in norm or norm in prev):
            return True
    return False


# ---------------------------------------------------------------------------
# Sexual content filter
# ---------------------------------------------------------------------------
# Beauty/fashion media frequently use words like "nude", "sexy", "sensual"
# in a purely cosmetic sense (nude lipstick, sexy red, sensual fragrance).
# We therefore only match terms that are virtually always explicit, or
# unambiguous phrases ("nude photos", "секс-скандал").

_SEXUAL_PATTERNS = [
    # Explicit English terms
    r"\bporn\w*",
    r"\bpornhub\b",
    r"\bonlyfans\b",
    r"\bxxx\b",
    r"\berotic\w*",
    r"\bhentai\b",
    r"\bfetish\w*",
    r"\bbdsm\b",
    r"\borgasm\w*",
    r"\bmasturbat\w*",
    r"\bintercourse\b",
    r"\brape\w*",
    r"\bincest\w*",
    r"\bpedoph\w*",
    r"\bgenitals?\b",
    r"\bsodom\w*",
    # Unambiguous English phrases
    r"\bsex (?:tape|scene|scenes|scandal|scandals|worker|workers|party|parties|"
    r"ring|rings|crime|crimes|offender|offenders|abuse|assault|trafficking|"
    r"trade|slavery|work|act|acts|addict|addiction|toy|toys|doll|dolls)\b",
    r"\bsexual (?:abuse|assault|harassment|misconduct|violence|predator|exploitation)\b",
    r"\bnude (?:photo|photos|pic|pics|picture|pictures|shoot|shoots|leak|leaks|"
    r"scene|scenes|selfie|selfies)\b",
    r"\bleaked nude\w*",
    r"\btopless (?:photo|photos|pic|pics|selfie|selfies|shoot|scene|scenes)\b",
    r"\bnaked (?:photo|photos|pic|pics|selfie|selfies|body|bodies)\b",
    # Russian explicit
    r"порн\w*",
    r"эрот\w*",
    r"разврат\w*",
    r"проститут\w*",
    r"бдсм",
    r"мастурб\w*",
    r"оргазм\w*",
    r"изнасил\w*",
    r"педофил\w*",
    r"садомаз\w*",
    r"\bпенис\w*",
    r"вагин\w+",
    r"гениталий|гениталии|гениталиям",
    # Russian phrases
    r"секс[- ]?скандал\w*",
    r"секс[- ]?тур\w*",
    r"секс[- ]?работ\w*",
    r"секс[- ]?шоп\w*",
    r"секс[- ]?вечеринк\w*",
    r"секс[- ]?рабын\w*",
    r"секс[- ]?игрушк\w*",
    r"интимн(?:ое|ые|ого|ым|ом|ой) (?:фото|видео|снимк|снимки|снимок|кадр|кадры|переписк)",
    r"откровенн(?:ое|ые|ых|ыми) (?:фото|видео|снимк|кадр)",
    r"обнажённ?(?:ая|ое|ые|ого|ых|ыми) (?:фото|натур|тело|снимк)",
    r"полов(?:ой|ые|ым) (?:акт|акты)",
    r"домогательств\w*",
]

_SEXUAL_RE = re.compile("|".join(_SEXUAL_PATTERNS), re.IGNORECASE)


def is_sexual_content(*texts: str) -> bool:
    """Return True if any of the given text fragments matches an explicit pattern."""
    for t in texts:
        if t and _SEXUAL_RE.search(t):
            return True
    return False


# ---------------------------------------------------------------------------
# Military / defense / war content filter
# ---------------------------------------------------------------------------
# Beauty/fashion media frequently use words that overlap with the military
# vocabulary in completely benign ways: "bath bomb", "bomber jacket",
# "tank top", "combat boots", "camo print", "trade war", "fashion army",
# "secret weapon", "ракета продаж". We therefore avoid bare roots like
# "war", "bomb", "weapon", "tank", "combat", "camo" — and only match
# unambiguous phrases that practically always indicate war / armed
# conflict / defense industry content.

_MILITARY_PATTERNS = [
    # ---- English: explicit military / war ----
    r"\bairstrike\w*",
    r"\bair[- ]strike\w*",
    r"\bmissile (?:strike|strikes|attack|attacks|launch|launches|test|tests|"
    r"barrage|salvo|fired|hit|hits|defen[cs]e|interceptor)\b",
    r"\bballistic missile\w*",
    r"\bcruise missile\w*",
    r"\bhypersonic missile\w*",
    r"\bnuclear (?:strike|weapon|weapons|war|warhead|warheads|arsenal|"
    r"threat|deterrent|deterrence|escalation|test|tests|fallout)\b",
    r"\bchemical weapon\w*",
    r"\bbiological weapon\w*",
    r"\bdirty bomb\w*",
    r"\bsuicide bomb\w+",
    r"\bcar bomb\w*",
    r"\bIED\b",
    r"\bRPG launcher\w*",
    r"\bgrenade launcher\w*",
    r"\bwar crime\w*",
    r"\bwar zone\w*",
    r"\bwar[- ]zone\w*",
    r"\bbattlefield\w*",
    r"\bbattlefront\w*",
    r"\bfront[- ]?line (?:fighter|fighters|soldier|soldiers|troop|troops|"
    r"fighting|combat|positions?|trenches?)\b",
    r"\bcombat (?:zone|zones|mission|missions|operation|operations|troops|"
    r"soldier|soldiers|patrol|patrols|fatigue|fatigues|casualt\w+)\b",
    r"\bground offensive\w*",
    r"\bcounter[- ]offensive\w*",
    r"\bmilitary (?:operation|operations|strike|strikes|aid|action|actions|"
    r"invasion|offensive|exercise|exercises|drill|drills|drone|drones|base|"
    r"bases|deployment|deployments|forces?|intervention|coup|conflict|"
    r"alliance|build[- ]?up|hardware|equipment|spending|budget|industrial|"
    r"complex|contractor|contractors|supplies|supply|technology|aircraft|"
    r"vehicle|vehicles|personnel|aid package|junta|parade)\b",
    r"\bdefen[cs]e (?:ministry|minister|contractor|contractors|industry|"
    r"spending|budget|deal|deals|company|companies|firm|firms|sector|"
    r"stock|stocks|supplier|suppliers|tech|production|exports?)\b",
    r"\barms (?:deal|deals|race|export|exports|import|imports|sale|sales|"
    r"supply|supplies|industry|manufacturer|manufacturers|shipment|"
    r"shipments|embargo|embargoes|smuggling|trafficking|dealer|dealers)\b",
    r"\bweapons (?:deal|deals|export|exports|import|imports|supply|supplies|"
    r"smuggling|trafficking|shipment|shipments|cache|stockpile|stockpiles|"
    r"manufacturer|manufacturers|program|programme|programs|programmes|"
    r"factory|factories|plant|plants)\b",
    r"\bartillery (?:shell|shells|fire|strike|strikes|barrage|battery|"
    r"batteries|attack|attacks|shelling|bombardment)\b",
    r"\bshelling\b",
    r"\bbombardment\w*",
    r"\bbombing (?:campaign|raid|raids|run|runs|attack|attacks)\b",
    r"\bdrone (?:strike|strikes|attack|attacks|warfare|swarm|swarms)\b",
    r"\bkamikaze drone\w*",
    r"\bPOW\b",
    r"\bprisoner[s]? of war\b",
    r"\bhostage[s]? (?:taking|crisis|deal|exchange|release|negotiations?)\b",
    r"\bceasefire\w*",
    r"\bcease[- ]fire\w*",
    r"\bmobilization\b",
    r"\bmobilisation\b",
    r"\bconscription\b",
    r"\binvading force\w*",
    r"\bmilitary invasion\b",
    r"\boccupied territor\w+",
    r"\bgenocide\b",
    r"\bethnic cleansing\b",
    # Specific weapons / systems
    r"\bF-?16\w*",
    r"\bF-?35\w*",
    r"\bHIMARS\b",
    r"\bPatriot (?:missile|missiles|battery|batteries|system|systems)\b",
    r"\bIron Dome\b",
    r"\bATACMS\b",
    r"\bJavelin missile\w*",
    r"\bStinger missile\w*",
    r"\bKalashnikov\w*",
    r"\bAK-?47\b",
    # Designated militant / terror groups in active-conflict reporting
    r"\bISIS\b",
    r"\bISIL\b",
    r"\bal[- ]qaeda\b",
    r"\bTaliban\b",
    r"\bHamas\b",
    r"\bHezbollah\b",
    r"\bHouthi\w*",
    r"\bIslamic State\b",
    r"\bWagner (?:group|mercenar)\w*",
    # Named conflicts
    r"\b(?:russia|russian)[- ]ukraine war\b",
    r"\bwar in ukraine\b",
    r"\bukraine war\b",
    r"\bgaza war\b",
    r"\bisrael[- ]hamas war\b",
    r"\bsyrian (?:civil )?war\b",
    r"\bcivil war\b",
    r"\bproxy war\b",
    r"\bworld war\b",
    # ---- Russian: explicit military / war ----
    r"военнослужащ\w+",
    r"военнопленн\w+",
    r"военкомат\w*",
    r"вооруж[её]нн\w* конфликт\w*",
    r"вооруж[её]нн\w* сил\w*",
    r"\bвооружени\w*",
    r"\bбоеприпас\w*",
    r"военн(?:ая|ой|ую) (?:операци|техник|промышленност|агресси|кампани|"
    r"реформ|доктрин|разведк|базой?)\w*",
    r"военн(?:ый|ого|ому|ом) (?:конфликт|удар|поход|бюджет|заказ|альянс|"
    r"парад|переворот|трибунал)\w*",
    r"военн(?:ые|ых|ыми) (?:действи|преступлени|учени|сбор|поставк|баз|"
    r"расход|формировани|подразделени)\w*",
    r"боев(?:ые|ых|ыми) действи\w*",
    r"боев(?:ое|ого|ому) дежурств\w*",
    r"оборонн(?:ая|ой|ую) (?:промышленност|отрасл|компани|сфер|стратеги)\w*",
    r"оборонн(?:ый|ого|ому) (?:заказ|комплекс|бюджет|сектор|концерн|"
    r"холдинг|завод)\w*",
    r"оборонк\w+",
    r"оборонпром\w*",
    r"\bВПК\b",
    r"министерств\w+ обороны",
    r"\bМинобороны\b",
    r"\bГенштаб\w*",
    r"\bПентагон\w*",
    r"ракетн(?:ый|ого|ому|ом) (?:удар|обстрел|комплекс|пуск|залп|атак)\w*",
    r"ракетн(?:ая|ой|ую) (?:атак|опасност|угроз|оборон|систем)\w*",
    r"ракетн(?:ые|ых|ыми) (?:удар|войск|обстрел|пуск)\w*",
    r"баллистическ\w* ракет\w*",
    r"крылат\w* ракет\w*",
    r"гиперзвуков\w* ракет\w*",
    r"ядерн(?:ая|ое|ой|ом|ый|ого|ому|ыми) (?:удар|оружи|война|угроз|"
    r"арсенал|боеголовк|заряд|бомб|испытани|шантаж|сдерживани|потенциал)\w*",
    r"химическ\w* оружи\w*",
    r"биологическ\w* оружи\w*",
    r"артобстрел\w*",
    r"артиллерийск\w* (?:обстрел|удар|бой|огонь|батаре)\w*",
    r"авиауд\w*",
    r"авианал[её]т\w*",
    r"бомбардиров\w*",
    r"бомб[её]жк\w*",
    r"наступлени\w+ (?:армии|войск|на)",
    r"контрнаступлени\w+",
    r"спецоперац\w+",
    r"\bСВО\b",
    r"мобилизац\w+",
    r"частичн\w* мобилизаци\w*",
    r"всеобщ\w* мобилизаци\w*",
    r"дезертирств\w*",
    r"военн\w+ эшелон\w*",
    r"военн\w+ техник\w*",
    r"бронетехник\w*",
    r"\bтерак\w+",
    r"террористическ\w* (?:атак|акт|нападени|групп|организаци|операци)\w*",
    r"\bТалибан\w*",
    r"\bХАМАС\w*",
    r"\bХезболл\w*",
    r"\bИГИЛ\w*",
    r"\bхуситов\b",
    r"\bхуситы\b",
    r"\bВагнер(?:а|у|ом) (?:групп|ЧВК|наёмник|наемник)\w*",
    r"война (?:в|с|на|против) [А-ЯA-Z][а-яa-z]+",
    r"войны (?:в|с|на) [А-ЯA-Z][а-яa-z]+",
    r"войн[уы] в Украин\w+",
    r"гражданск\w* войн\w*",
    r"мировая войн\w*",
    r"перв\w* мирова\w*",
    r"втор\w* мирова\w*",
    r"холодн\w* войн\w*",
    r"\bдрон[- ]камикадзе\w*",
    r"ударн\w* дрон\w*",
    r"беспилотн\w* (?:удар|атак|налёт|налет)\w*",
    r"оккупаци\w+",
    r"оккупирован\w+ (?:террит|город|регион|посёлк|посел)\w*",
    r"линия фронта",
    r"линии фронта",
    r"фронтов\w* (?:сводк|зон|обстрел|боев|линии)\w*",
    r"геноцид\w*",
    r"этническ\w* чистк\w*",
    r"\bЛНР\b",
    r"\bДНР\b",
    r"\bСБУ\b",
    r"\bФСБ\b",
    r"\bЦАХАЛ\w*",
    # ---- War-adjacent: occupied territories / civilian casualties / press in conflict ----
    r"\boccupied palestin\w*",
    r"\bgaza (?:strip|war|conflict|crisis|hospital|hospitals|civilian|civilians|"
    r"casualt\w+|death|deaths|killed|under attack|under fire|bombardment|"
    r"siege|blockade|truce|ceasefire|aid|reconstruction)\b",
    r"\bwest bank (?:settlement|settlements|raid|raids|incursion|incursions|"
    r"operation|operations|crackdown|crackdowns|protest|protests|clashes)\b",
    r"\bcivilian (?:casualt\w+|death|deaths|killed|wounded|injured|displaced|"
    r"targeted|under fire|under attack|killings)\b",
    r"\b(?:journalist|journalists|press|media|hospital|hospitals|school|"
    r"schools|aid worker|aid workers|paramedic|paramedics) "
    r"(?:killed|wounded|targeted|attacked|under fire|under attack|displaced|"
    r"detained|abducted)\b",
    r"\battacks? on (?:journalist|journalists|press|media|hospital|hospitals|"
    r"school|schools|aid worker|aid workers|civilian|civilians)\b",
    r"\bpress freedom in (?:occupied|gaza|ukraine|russia|iran|syria|yemen|"
    r"war|conflict|wartime|china)\b",
    r"\bmedia infrastructure (?:destruction|attack|damage|targeting|destroyed|"
    r"attacked|damaged|targeted|wiped out)\b",
    r"\bhumanitarian (?:crisis|catastrophe|disaster|corridor|corridors|"
    r"truce|pause|ceasefire)\b",
    r"\brefugee crisis\b",
    r"\binternally displaced (?:person|persons|people|civilian|civilians)\b",
    r"\bdeath toll (?:in|from|of|after) (?:gaza|ukraine|war|strike|strikes|"
    r"attack|attacks|conflict|bombing|airstrike|airstrikes|raid|raids)\b",
    r"\bmass grave\w*",
    r"\bwar[- ]affected\b",
    r"\boccupying force\w*",
    r"\bsettler violence\b",
    r"\bwar reporter\w*",
    r"\bcombat journalis\w*",
    r"\bwomen and children killed\b",
    r"оккупирован\w+ палестинск\w+",
    r"палестинск\w+ территори\w+",
    r"\bсектор\w* Газ[ыеау]\b",
    r"в Газ[еыау] (?:под|удар|обстрел|жертв|погиб|убит|разруш|гуманитар|"
    r"конфликт|войн|перемир|больниц|госпитал)\w*",
    r"удар\w+ по Газ[еыау]\b",
    r"обстрел\w+ Газ[ыеау]\b",
    r"журналист\w+ (?:погиб|убит|ранен|задержан|арестован|пропал|убива)\w*",
    r"нападени\w+ на (?:журналист|медиа|пресс|редакци|больниц|школ|"
    r"госпитал|мирн|гражданск)\w+",
    r"разрушени\w+ медиа[- ]инфраструктур\w+",
    r"уничтожени\w+ медиа[- ]инфраструктур\w+",
    r"свобод\w+ прессы в (?:Газе|Газу|Газы|оккупир|зон\w* конфликт|"
    r"зон\w* войн|Украин|России|Иран|Сири|Кита)\w*",
    r"цензур\w+ в (?:зон\w+ (?:конфликт|войн|бо)|оккупир)\w*",
    r"мирн\w+ жител\w+ (?:погиб|убит|ранен|пострада|расстрел)\w*",
    r"гражданск\w+ (?:жертв|потер|погибш|убит|ранен|расстрел)\w*",
    r"беженц\w+ из зон\w+ (?:конфликт|войн|бо)\w*",
    r"вынужденн\w+ переселенц\w+",
    r"гуманитарн\w+ (?:катастроф|кризис|корид|перемир|пауз)\w*",
    r"массов\w+ захоронени\w+",
    r"число (?:погибш|жертв|убит|ранен) (?:среди мирных|в Газ|в Украин|"
    r"в результате удар|в результате обстрел|в результате авиауд|"
    r"в результате бомб)\w*",
    r"поселенческ\w+ насили\w+",
    r"военн\w+ корреспондент\w+",
]

_MILITARY_RE = re.compile("|".join(_MILITARY_PATTERNS), re.IGNORECASE)


def is_military_content(*texts: str) -> bool:
    """Return True if any of the given text fragments matches a military / war / defense pattern."""
    for t in texts:
        if t and _MILITARY_RE.search(t):
            return True
    return False


# ---------------------------------------------------------------------------
# Vague-content filter
# ---------------------------------------------------------------------------
# Reject articles that are pure platitudes — no specific people, brands,
# universities, numbers. A post needs at least 2 distinct concrete tokens:
#   - digit run (year, percentage, dollar amount, count) — "$5B", "2026", "78%"
#   - Title-case word ≥3 chars not on the platitude stoplist — "Apple", "Цермело"
#   - all-caps abbreviation ≥2 chars — "США", "ИИ", "AI", "MCP", "IBM"
# Bilingual: works for English RSS source AND Russian source.
#
# Examples that get rejected:
#   "Технологическая индустрия в основном ориентирована на США..."  (only "США")
#   "Искусственный интеллект открывает новые горизонты для бизнеса..." (no entities)
#   "Тридцать шесть лет спустя писатель возвращается..." (spelled-out number)
#   "Researchers find new way to improve mood" (only stopword tokens)

_DIGIT_RE = re.compile(r"\d+")
_CAP_WORD_RE = re.compile(r"\b[A-ZА-ЯЁ][A-Za-zА-ЯЁа-яё]{2,}\b")
_ABBREV_RE = re.compile(r"\b[A-ZА-ЯЁ]{2,}\b")

# Platitude stopwords — capitalized at the start of vague summary sentences.
# When these are the ONLY "named" tokens, the article is meaningless.
_VAGUE_WORDS = {
    # Russian collective nouns / generic adjectives / fillers
    "учёные", "ученые", "исследователи", "эксперты", "специалисты",
    "врачи", "медики", "психологи", "нейробиологи", "химики", "физики",
    "биологи", "астрономы", "разработчики", "инженеры", "программисты",
    "аналитики", "социологи", "экономисты", "археологи", "журналисты",
    "научные", "научный", "научная", "научное",
    "новое", "новая", "новый", "новые", "новых", "новым", "нового",
    "это", "этот", "эта", "эти", "этом", "этой", "этого",
    "своё", "свой", "своя", "свои", "своих",
    "технологическая", "технологический", "технологические", "технологическое",
    "исторические", "исторический", "историческая", "историческое",
    "однако", "важно", "стоит", "сегодня", "сейчас", "теперь",
    "вчера", "завтра", "позже", "раньше",
    "все", "всё", "всех", "всем", "всеми",
    "большинство", "многое", "многие", "многих",
    "искусственный", "искусственная", "искусственного", "искусственному",
    "интеллект", "правительство", "общество", "пользователи",
    "мифический", "мифическая", "мифическое",
    "обычный", "обычная", "обычные", "обычное",
    "глобальный", "глобальная", "глобальное", "глобальные", "глобальных",
    "интересный", "интересная", "интересное", "интересные",
    "будущее", "будущая", "будущий", "будущему",
    "ситуация", "проблема", "вопрос", "тема",
    "первый", "первая", "первое", "первые",
    "второй", "вторая", "второе", "вторые",
    "третий", "третья", "третье", "третьи",
    "тридцать", "сорок", "пятьдесят", "семьдесят", "восемьдесят", "девяносто",
    "сотни", "тысячи", "миллионы", "миллиарды",
    # English fillers & generic nouns
    "the", "and", "but", "however", "today", "tomorrow", "yesterday",
    "researchers", "scientists", "experts", "specialists",
    "developers", "engineers", "people", "users", "consumers",
    "new", "this", "that", "these", "those", "now", "then",
    "global", "future", "modern", "important", "interesting",
    "company", "companies", "industry", "industries",
    "world", "society", "government",
}


def is_vague_content(title: str, description: str) -> bool:
    """Return True if fewer than 2 distinct concrete tokens are present."""
    text = f"{title or ''}. {description or ''}".strip()
    if not text:
        return True

    concrete: set[str] = set()

    for m in _DIGIT_RE.finditer(text):
        concrete.add("_d_" + m.group())

    for m in _CAP_WORD_RE.finditer(text):
        w = m.group()
        if w.lower() not in _VAGUE_WORDS:
            concrete.add(w.lower())

    for m in _ABBREV_RE.finditer(text):
        concrete.add(m.group().lower())

    return len(concrete) < 2


# ---------------------------------------------------------------------------
# Caption builder
# ---------------------------------------------------------------------------

def format_russian_article(title: str, description: str) -> str:
    """
    Build a Telegram HTML post from a Russian-language RSS item
    without sending it to OpenAI. Title becomes bold; description
    is stripped of HTML and kept as plain text below.
    """
    import html
    import re

    def _strip(s: str) -> str:
        s = re.sub(r"<[^>]+>", "", s or "")
        s = html.unescape(s)
        return re.sub(r"\s+", " ", s).strip()

    clean_title = _strip(title)
    clean_desc = _strip(description)

    # Escape &, <, > in plain text for Telegram HTML parse mode
    def _esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    bold_title = f"<b>{_esc(clean_title)}</b>"
    if clean_desc and clean_desc != clean_title:
        return f"{bold_title}\n\n{_esc(clean_desc)}"
    return bold_title


def build_caption(text: str, url: str, hashtags: str) -> str:
    """
    Format:
        {translated text with HTML formatting}

        <a href="url">Читать далее →</a>

        #хэштеги
    """
    link_line = f'<a href="{url}">Читать далее →</a>'
    suffix = f"\n\n{link_line}\n\n{hashtags}"
    # text already contains Telegram HTML tags from the model — do not escape
    max_text = CAPTION_LIMIT - len(suffix)
    if len(text) > max_text:
        text = text[: max_text - 1] + "…"
    return text + suffix


# ---------------------------------------------------------------------------
# Telegram sender
# ---------------------------------------------------------------------------

async def send_photo(image_data: bytes, caption: str) -> bool:
    url = TG_API.format(token=config.TELEGRAM_BOT_TOKEN, method="sendPhoto")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url,
                data={"chat_id": config.TELEGRAM_CHANNEL_ID, "caption": caption, "parse_mode": "HTML"},
                files={"photo": ("photo.jpg", image_data, "image/jpeg")},
            )
        if resp.status_code == 200 and resp.json().get("ok"):
            return True
        logger.error("Telegram error: %s", resp.text)
        return False
    except Exception as e:
        logger.error("Telegram exception: %s", e)
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _leading_tag(hashtags: str) -> str:
    """Return the first hashtag as a rough category key."""
    return hashtags.split()[0] if hashtags else ""


async def _post_one(posted_urls: set, posted_titles: set, recent_tags: list) -> bool:
    """Try each feed (shuffled) until we post one article. Returns True on success.

    Feeds whose leading hashtag matches one of the last 3 posted categories
    are moved to the end so topics naturally alternate.
    """
    feeds = RSS_FEEDS.copy()
    random.shuffle(feeds)

    # Deprioritise recently-used categories
    recent = set(recent_tags[-3:])
    feeds.sort(key=lambda f: 1 if _leading_tag(f["hashtags"]) in recent else 0)

    for feed in feeds:
        article = await find_article_with_image(feed, posted_urls)
        if not article:
            continue

        # Skip if title too similar to an already-posted article
        if is_title_duplicate(article["title"], posted_titles):
            logger.info("Skipped duplicate title: %s", article["title"])
            posted_urls.add(article["url"])
            save_posted(article["url"])
            continue

        # Skip sexually explicit content
        if is_sexual_content(article["title"], article["description"], article["url"]):
            logger.info("Skipped sexual content: %s", article["title"])
            posted_urls.add(article["url"])
            save_posted(article["url"])
            continue

        # Skip war / military / defense industry content
        if is_military_content(article["title"], article["description"], article["url"]):
            logger.info("Skipped military content: %s", article["title"])
            posted_urls.add(article["url"])
            save_posted(article["url"])
            continue

        # Skip vague articles with no concrete entities (names, numbers, brands)
        if is_vague_content(article["title"], article["description"]):
            logger.info("Skipped vague content: %s", article["title"])
            posted_urls.add(article["url"])
            save_posted(article["url"])
            continue

        try:
            if feed.get("lang") == "ru":
                # Russian-language feed: skip OpenAI translation, format locally.
                # Skipping the translator also skips its relevance rule, so ask
                # separately — general-business RU feeds carry lots of off-topic news.
                if not await is_on_topic(article["title"], article["description"]):
                    logger.info("Skipped off-topic: %s", article["title"])
                    posted_urls.add(article["url"])
                    save_posted(article["url"])
                    continue
                text = format_russian_article(article["title"], article["description"])
            else:
                text = await translate_article(article["title"], article["description"])
                # Translator may return __SKIP__ for vague platitudes /
                # mashed-up multi-topic posts / off-limit topics.
                if text.strip().startswith("__SKIP__"):
                    logger.info("Translator skipped: %s", article["title"])
                    posted_urls.add(article["url"])
                    save_posted(article["url"])
                    continue
                # Re-check translated text — catches English-source platitudes
                # that slipped past the original-text filter.
                if is_vague_content(article["title"], text):
                    logger.info("Skipped vague translated text: %s", article["title"])
                    posted_urls.add(article["url"])
                    save_posted(article["url"])
                    continue
                text = await add_wiki_links(text)
            caption = build_caption(text, article["url"], article["hashtags"])
            ok = await send_photo(article["image_data"], caption)
            # Always mark as seen to avoid infinite retry on bad images
            posted_urls.add(article["url"])
            posted_titles.add(_normalize_title(article["title"]))
            save_posted(article["url"], article["title"], update_time=ok)
            if ok:
                logger.info("Posted: %s", article["title"])
                recent_tags.append(_leading_tag(feed["hashtags"]))
                return True
            logger.warning("Skipped (Telegram rejected): %s", article["title"])
        except Exception as e:
            logger.error("Error posting %s: %s", article.get("url"), e)

    logger.warning("No new articles with images found across all feeds.")
    return False


def _parse_time(t: str) -> dtime:
    h, m = t.split(":")
    return dtime(int(h), int(m))


def _quiet_sleep_seconds() -> float | None:
    """Return seconds to sleep if we're in quiet hours, else None."""
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    quiet_start = _parse_time(config.QUIET_START)
    quiet_end   = _parse_time(config.QUIET_END)

    current = now.time().replace(second=0, microsecond=0)

    # Quiet window spans midnight (e.g. 22:00 – 08:00)
    in_quiet = (
        current >= quiet_start or current < quiet_end
        if quiet_start > quiet_end
        else quiet_start <= current < quiet_end
    )

    if not in_quiet:
        return None

    # Sleep until quiet_end today or tomorrow
    resume = datetime.combine(now.date(), quiet_end, tzinfo=now.tzinfo)
    if resume <= now:
        resume += timedelta(days=1)
    return (resume - now).total_seconds()


async def post_once() -> bool:
    """Post a single news item and exit. For use with GitHub Actions cron."""
    # Triggers can arrive late (GitHub cron has been running hours behind
    # since Aug 2026), so a run can land at night. Never post in quiet hours.
    if _quiet_sleep_seconds() is not None:
        logger.info(
            "news-once: quiet hours (%s–%s %s). Skipping.",
            config.QUIET_START, config.QUIET_END, config.TIMEZONE,
        )
        return False

    # Enforce minimum interval between posts (GitHub Actions cron is imprecise)
    elapsed = _seconds_since_last_post()
    if elapsed is not None and elapsed < POST_INTERVAL:
        logger.info(
            "news-once: only %d sec since last post (min %d). Skipping.",
            int(elapsed), POST_INTERVAL,
        )
        return False

    posted_urls = load_posted_urls()
    posted_titles = load_posted_titles()
    recent_tags: list = []
    ok = await _post_one(posted_urls, posted_titles, recent_tags)
    if ok:
        logger.info("news-once: posted successfully.")
    else:
        logger.warning("news-once: no article posted.")
    return ok


async def run_news_bot() -> None:
    """Infinite loop: post one news item every 5 minutes (paused during quiet hours)."""
    logger.info("RSS News Bot started. Posting every %d minutes.", POST_INTERVAL // 60)
    logger.info("Quiet hours: %s – %s", config.QUIET_START, config.QUIET_END)
    posted_urls = load_posted_urls()
    posted_titles = load_posted_titles()
    recent_tags: list = []

    while True:
        sleep_sec = _quiet_sleep_seconds()
        if sleep_sec is not None:
            logger.info(
                "Quiet hours — pausing until %s.",
                config.QUIET_END,
            )
            # Sleep in short intervals to survive macOS sleep/wake
            while _quiet_sleep_seconds() is not None:
                await asyncio.sleep(30)
            logger.info("Quiet hours ended, resuming.")
            continue

        try:
            await _post_one(posted_urls, posted_titles, recent_tags)
        except Exception as e:
            logger.error("Unexpected bot error: %s", e)
        await asyncio.sleep(POST_INTERVAL)
