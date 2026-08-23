"""
Playerok Parser Bot
====================
Парсит разделы Playerok.com и ищет лоты с низкой конкуренцией.

Установка зависимостей:
    pip install aiogram aiohttp beautifulsoup4 lxml

Запуск:
    python playerok_bot.py
"""

import asyncio
import logging
import re
from datetime import datetime
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from bs4 import BeautifulSoup

# ─── НАСТРОЙКИ ───────────────────────────────────────────────────────────────
BOT_TOKEN = "8657083122:AAFfn-iGiiVKMYsBVkrWHBqZk0hNchHNmrY"  # Токен от @BotFather
YOUR_CHAT_ID = None               # Заполнится автоматически при первом /start

# ─── ЛОГИРОВАНИЕ ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─── FSM СОСТОЯНИЯ ───────────────────────────────────────────────────────────
class SetupStates(StatesGroup):
    waiting_url = State()
    waiting_cooldown = State()
    waiting_min_rating = State()
    waiting_max_competitors = State()

# ─── ХРАНИЛИЩЕ ЗАДАЧ ─────────────────────────────────────────────────────────
tasks: dict[int, asyncio.Task] = {}  # chat_id -> asyncio Task
configs: dict[int, dict] = {}        # chat_id -> конфиг

# ─── ПАРСЕР ──────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


async def fetch_page(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    """Загружает страницу и возвращает HTML."""
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 200:
                return await r.text()
            log.warning(f"HTTP {r.status} для {url}")
    except Exception as e:
        log.error(f"Ошибка загрузки {url}: {e}")
    return None


def parse_lots(html: str) -> list[dict]:
    """Извлекает лоты из HTML страницы Playerok."""
    soup = BeautifulSoup(html, "lxml")
    lots = []

    cards = (
        soup.select("div[class*='lot-card']") or
        soup.select("div[class*='LotCard']") or
        soup.select("article[class*='lot']") or
        soup.select("div[class*='offer']") or
        soup.select("a[href*='/lot/']")
    )

    for card in cards:
        try:
            lot = {}

            title_el = (
                card.select_one("[class*='title']") or
                card.select_one("h2") or
                card.select_one("h3") or
                card.select_one("p")
            )
            lot["title"] = title_el.get_text(strip=True) if title_el else "Без названия"

            price_el = card.select_one("[class*='price']")
            if price_el:
                price_text = price_el.get_text(strip=True)
                digits = re.findall(r"\d+", price_text.replace(" ", ""))
                lot["price"] = int("".join(digits)) if digits else 0
            else:
                lot["price"] = 0

            link_el = card.select_one("a") or card
            href = link_el.get("href", "")
            if href and not href.startswith("http"):
                href = "https://playerok.com" + href
            lot["url"] = href

            rating_el = card.select_one("[class*='rating']") or card.select_one("[class*='Rating']")
            if rating_el:
                rating_text = rating_el.get_text(strip=True)
                digits = re.findall(r"[\d.]+", rating_text)
                lot["seller_rating"] = float(digits[0]) if digits else 0.0
            else:
                lot["seller_rating"] = 0.0

            reviews_el = card.select_one("[class*='review']") or card.select_one("[class*='sold']")
            if reviews_el:
                nums = re.findall(r"\d+", reviews_el.get_text())
                lot["seller_reviews"] = int(nums[0]) if nums else 0
            else:
                lot["seller_reviews"] = 0

            if lot["title"] and lot["url"]:
                lots.append(lot)
        except Exception as e:
            log.debug(f"Ошибка парсинга карточки: {e}")

    return lots


def analyze_competition(lots: list[dict], query_title: str) -> list[dict]:
    from collections import Counter

    def normalize(title: str) -> str:
        title = re.sub(r"\d+\s*(руб|₽|rub|\$|usd)", "", title, flags=re.IGNORECASE)
        title = re.sub(r"\s+", " ", title)
        return title.lower().strip()

    normalized = [normalize(lot["title"]) for lot in lots]
    counts = Counter(normalized)

    result = []
    seen = set()
    for lot, norm in zip(lots, normalized):
        if norm in seen:
            continue
        seen.add(norm)
        lot_copy = dict(lot)
        lot_copy["competitors"] = counts[norm] - 1
        result.append(lot_copy)

    return result


async def parse_and_notify(bot: Bot, chat_id: int, cfg: dict):
    url = cfg["url"]
    cooldown = cfg["cooldown"]
    min_rating = cfg["min_rating"]
    max_competitors = cfg["max_competitors"]

    known_urls: set[str] = set()

    log.info(f"[{chat_id}] Парсинг запущен: {url}")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                html = await fetch_page(session, url)
                if not html:
                    await asyncio.sleep(cooldown * 60)
                    continue

                lots = parse_lots(html)
                if not lots:
                    await bot.send_message(
                        chat_id,
                        "⚠️ Не удалось найти лоты на странице.\n"
                        "Playerok использует JavaScript-рендеринг, попробуй проверить URL.\n"
                        "Следующая проверка через {} мин.".format(cooldown)
                    )
                    await asyncio.sleep(cooldown * 60)
                    continue

                analyzed = analyze_competition(lots, "")

                good_lots = [
                    l for l in analyzed
                    if l.get("seller_rating", 0) >= min_rating
                    and l.get("competitors", 999) <= max_competitors
                    and l["url"] not in known_urls
                    and l["url"]
                ]

                if good_lots:
                    for lot in good_lots[:10]:
                        known_urls.add(lot["url"])
                        text = (
                            f"🎯 <b>Интересный лот!</b>\n\n"
                            f"📦 <b>{lot['title']}</b>\n"
                            f"💰 Цена: {lot['price']:,} ₽\n"
                            f"⭐ Рейтинг продавца: {lot['seller_rating']}\n"
                            f"👥 Конкурентов: {lot['competitors']}\n"
                            f"🔗 <a href=\"{lot['url']}\">Открыть лот</a>"
                        )
                        await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
                        await asyncio.sleep(0.5)

                await asyncio.sleep(cooldown * 60)

            except asyncio.CancelledError:
                log.info(f"[{chat_id}] Парсинг остановлен.")
                break
            except Exception as e:
                log.error(f"[{chat_id}] Ошибка: {e}")
                await asyncio.sleep(30)


# ─── HANDLERS ────────────────────────────────────────────────────────────────
dp = Dispatcher(storage=MemoryStorage())


@dp.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    text = (
        "👋 <b>Playerok Parser Bot</b>\n\n"
        "Я ищу лоты с низкой конкуренцией на Playerok.com\n\n"
        "Команды:\n"
        "🔍 /setup — настроить парсинг\n"
        "▶️ /start_parse — запустить парсинг\n"
        "⏹ /stop — остановить парсинг\n"
        "📊 /status — текущий статус\n"
    )
    await msg.answer(text, parse_mode="HTML")


@dp.message(Command("setup"))
async def cmd_setup(msg: Message, state: FSMContext):
    await state.set_state(SetupStates.waiting_url)
    await msg.answer(
        "🔗 <b>Шаг 1/4</b>\n\nОтправь ссылку на раздел Playerok для парсинга.\n\n"
        "Пример: <code>https://playerok.com/games/dota-2</code>",
        parse_mode="HTML"
    )


@dp.message(SetupStates.waiting_url)
async def process_url(msg: Message, state: FSMContext):
    url = msg.text.strip()
    if not url.startswith("http"):
        await msg.answer("❌ Это не похоже на ссылку. Отправь полный URL (начинается с https://)")
        return
    await state.update_data(url=url)
    await state.set_state(SetupStates.waiting_cooldown)
    await msg.answer(
        "⏱ <b>Шаг 2/4</b>\n\nКаждые сколько минут проверять?\n\n"
        "Рекомендую: 5-15 минут\n"
        "Отправь число (например: <code>10</code>)",
        parse_mode="HTML"
    )


@dp.message(SetupStates.waiting_cooldown)
async def process_cooldown(msg: Message, state: FSMContext):
    try:
        cd = int(msg.text.strip())
        if cd < 1 or cd > 1440:
            raise ValueError
    except ValueError:
        await msg.answer("❌ Введи число от 1 до 1440 (минут)")
        return
    await state.update_data(cooldown=cd)
    await state.set_state(SetupStates.waiting_min_rating)
    await msg.answer(
        "⭐ <b>Шаг 3/4</b>\n\nМинимальный рейтинг продавца?\n\n"
        "Например: <code>4.5</code> или <code>0</code> чтобы не фильтровать",
        parse_mode="HTML"
    )


@dp.message(SetupStates.waiting_min_rating)
async def process_rating(msg: Message, state: FSMContext):
    try:
        rating = float(msg.text.strip().replace(",", "."))
        if rating < 0 or rating > 5:
            raise ValueError
    except ValueError:
        await msg.answer("❌ Введи число от 0 до 5 (например: 4.5)")
        return
    await state.update_data(min_rating=rating)
    await state.set_state(SetupStates.waiting_max_competitors)
    await msg.answer(
        "👥 <b>Шаг 4/4</b>\n\nМаксимальное количество конкурентов (похожих лотов)?\n\n"
        "Например: <code>3</code> — показывать лоты где меньше 3 похожих",
        parse_mode="HTML"
    )


@dp.message(SetupStates.waiting_max_competitors)
async def process_max_competitors(msg: Message, state: FSMContext):
    try:
        max_c = int(msg.text.strip())
        if max_c < 0:
            raise ValueError
    except ValueError:
        await msg.answer("❌ Введи число больше 0")
        return

    data = await state.get_data()
    all_data = {**data, "max_competitors": max_c}
    configs[msg.chat.id] = all_data
    await state.clear()

    await msg.answer(
        f"✅ <b>Настройки сохранены!</b>\n\n"
        f"🔗 URL: <code>{all_data['url']}</code>\n"
        f"⏱ Кулдаун: {all_data['cooldown']} мин.\n"
        f"⭐ Мин. рейтинг: {all_data['min_rating']}\n"
        f"👥 Макс. конкурентов: {max_c}\n\n"
        f"Запусти парсинг командой /start_parse",
        parse_mode="HTML"
    )


@dp.message(Command("start_parse"))
async def cmd_start_parse(msg: Message):
    chat_id = msg.chat.id
    if chat_id not in configs:
        await msg.answer("⚠️ Сначала настрой парсинг через /setup")
        return
    if chat_id in tasks and not tasks[chat_id].done():
        await msg.answer("▶️ Парсинг уже запущен! Используй /stop чтобы остановить.")
        return
    task = asyncio.create_task(parse_and_notify(msg.bot, chat_id, configs[chat_id]))
    tasks[chat_id] = task
    await msg.answer(
        f"🚀 <b>Парсинг запущен!</b>\n\nПроверяю каждые {configs[chat_id]['cooldown']} мин. 👀",
        parse_mode="HTML"
    )


@dp.message(Command("stop"))
async def cmd_stop(msg: Message):
    chat_id = msg.chat.id
    if chat_id in tasks and not tasks[chat_id].done():
        tasks[chat_id].cancel()
        await msg.answer("⏹ Парсинг остановлен.")
    else:
        await msg.answer("ℹ️ Парсинг не был запущен.")


@dp.message(Command("status"))
async def cmd_status(msg: Message):
    chat_id = msg.chat.id
    if chat_id not in configs:
        await msg.answer("⚠️ Настройки не заданы. Используй /setup")
        return
    cfg = configs[chat_id]
    status = "▶️ Работает" if (chat_id in tasks and not tasks[chat_id].done()) else "⏹ Остановлен"
    await msg.answer(
        f"📊 <b>Статус</b>: {status}\n\n"
        f"🔗 URL: <code>{cfg['url']}</code>\n"
        f"⏱ Кулдаун: {cfg['cooldown']} мин.\n"
        f"⭐ Мин. рейтинг: {cfg['min_rating']}\n"
        f"👥 Макс. конкурентов: {cfg['max_competitors']}",
        parse_mode="HTML"
    )


# ─── ЗАПУСК ──────────────────────────────────────────────────────────────────
async def main():
    bot = Bot(token=BOT_TOKEN)
    log.info("Бот запущен!")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
