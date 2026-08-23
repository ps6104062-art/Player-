"""
Playerok Parser Bot (Playwright edition)
=========================================
Установка:
    pip install aiogram>=3.10.0 playwright beautifulsoup4 lxml
    playwright install chromium

Запуск:
    python bot.py
"""

import asyncio
import logging
import re
from collections import Counter
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Browser

BOT_TOKEN = "8657083122:AAFfn-iGiiVKMYsBVkrWHBqZk0hNchHNmrY"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

class SetupStates(StatesGroup):
    waiting_url = State()
    waiting_cooldown = State()
    waiting_min_rating = State()
    waiting_max_competitors = State()

tasks: dict[int, asyncio.Task] = {}
configs: dict[int, dict] = {}
browser: Optional[Browser] = None

async def fetch_with_playwright(url: str) -> Optional[str]:
    global browser
    try:
        page = await browser.new_page()
        await page.set_extra_http_headers({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        })
        await page.goto(url, wait_until="networkidle", timeout=30000)
        try:
            await page.wait_for_selector("a[href*='/lot/']", timeout=10000)
        except Exception:
            pass
        html = await page.content()
        await page.close()
        return html
    except Exception as e:
        log.error(f"Playwright ошибка: {e}")
        try:
            await page.close()
        except Exception:
            pass
        return None


def parse_lots(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    lots = []
    links = soup.select("a[href*='/lot/']")

    for link in links:
        try:
            href = link.get("href", "")
            if not href:
                continue
            if not href.startswith("http"):
                href = "https://playerok.com" + href

            title_el = (
                link.select_one("[class*='title']") or
                link.select_one("[class*='name']") or
                link.select_one("h2") or
                link.select_one("h3") or
                link.select_one("p")
            )
            title = title_el.get_text(strip=True) if title_el else link.get_text(strip=True)
            title = title.strip()
            if not title or len(title) < 3:
                continue

            price_el = link.select_one("[class*='price']") or link.select_one("[class*='Price']")
            price = 0
            if price_el:
                digits = re.findall(r"\d+", price_el.get_text().replace(" ", ""))
                price = int("".join(digits[:2])) if digits else 0

            rating_el = link.select_one("[class*='rating']") or link.select_one("[class*='Rating']")
            seller_rating = 0.0
            if rating_el:
                nums = re.findall(r"[\d.]+", rating_el.get_text())
                seller_rating = float(nums[0]) if nums else 0.0

            lots.append({
                "title": title,
                "price": price,
                "url": href,
                "seller_rating": seller_rating,
            })
        except Exception as e:
            log.debug(f"Ошибка карточки: {e}")

    return lots


def analyze_competition(lots: list[dict]) -> list[dict]:
    def normalize(title: str) -> str:
        t = re.sub(r"\d+\s*(руб|₽|rub|\$|usd|coins?)", "", title, flags=re.IGNORECASE)
        t = re.sub(r"\d+", "", t)
        t = re.sub(r"\s+", " ", t)
        return t.lower().strip()

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

    while True:
        try:
            html = await fetch_with_playwright(url)
            if not html:
                await asyncio.sleep(cooldown * 60)
                continue

            lots = parse_lots(html)
            if not lots:
                await bot.send_message(chat_id, f"⚠️ Лоты не найдены. Следующая проверка через {cooldown} мин.")
                await asyncio.sleep(cooldown * 60)
                continue

            analyzed = analyze_competition(lots)
            good_lots = [
                l for l in analyzed
                if l.get("seller_rating", 0) >= min_rating
                and l.get("competitors", 999) <= max_competitors
                and l["url"] not in known_urls
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
            break
        except Exception as e:
            log.error(f"[{chat_id}] Ошибка: {e}")
            await asyncio.sleep(30)


dp = Dispatcher(storage=MemoryStorage())


@dp.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(
        "👋 <b>Playerok Parser Bot</b>\n\n"
        "🔍 /setup — настроить\n"
        "▶️ /start_parse — запустить\n"
        "⏹ /stop — остановить\n"
        "📊 /status — статус",
        parse_mode="HTML"
    )


@dp.message(Command("setup"))
async def cmd_setup(msg: Message, state: FSMContext):
    await state.set_state(SetupStates.waiting_url)
    await msg.answer(
        "🔗 <b>Шаг 1/4</b>\n\nОтправь ссылку на раздел Playerok.\n\n"
        "Пример: <code>https://playerok.com/rust/other</code>",
        parse_mode="HTML"
    )


@dp.message(SetupStates.waiting_url)
async def process_url(msg: Message, state: FSMContext):
    url = msg.text.strip()
    if not url.startswith("http"):
        await msg.answer("❌ Нужен полный URL (https://...)")
        return
    await state.update_data(url=url)
    await state.set_state(SetupStates.waiting_cooldown)
    await msg.answer("⏱ <b>Шаг 2/4</b>\n\nКаждые сколько минут проверять?\nПример: <code>5</code>", parse_mode="HTML")


@dp.message(SetupStates.waiting_cooldown)
async def process_cooldown(msg: Message, state: FSMContext):
    try:
        cd = int(msg.text.strip())
        if cd < 1 or cd > 1440:
            raise ValueError
    except ValueError:
        await msg.answer("❌ Введи число от 1 до 1440")
        return
    await state.update_data(cooldown=cd)
    await state.set_state(SetupStates.waiting_min_rating)
    await msg.answer("⭐ <b>Шаг 3/4</b>\n\nМин. рейтинг продавца?\nПример: <code>4.5</code> или <code>0</code>", parse_mode="HTML")


@dp.message(SetupStates.waiting_min_rating)
async def process_rating(msg: Message, state: FSMContext):
    try:
        rating = float(msg.text.strip().replace(",", "."))
        if rating < 0 or rating > 5:
            raise ValueError
    except ValueError:
        await msg.answer("❌ Введи число от 0 до 5")
        return
    await state.update_data(min_rating=rating)
    await state.set_state(SetupStates.waiting_max_competitors)
    await msg.answer("👥 <b>Шаг 4/4</b>\n\nМакс. конкурентов?\nПример: <code>5</code>", parse_mode="HTML")


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
        f"🔗 {all_data['url']}\n"
        f"⏱ Каждые {all_data['cooldown']} мин.\n"
        f"⭐ Мин. рейтинг: {all_data['min_rating']}\n"
        f"👥 Макс. конкурентов: {max_c}\n\n"
        f"Запускай: /start_parse",
        parse_mode="HTML"
    )


@dp.message(Command("start_parse"))
async def cmd_start_parse(msg: Message):
    chat_id = msg.chat.id
    if chat_id not in configs:
        await msg.answer("⚠️ Сначала /setup")
        return
    if chat_id in tasks and not tasks[chat_id].done():
        await msg.answer("▶️ Уже запущен! /stop чтобы остановить.")
        return
    task = asyncio.create_task(parse_and_notify(msg.bot, chat_id, configs[chat_id]))
    tasks[chat_id] = task
    await msg.answer(f"🚀 <b>Запущен!</b> Проверяю каждые {configs[chat_id]['cooldown']} мин. 👀", parse_mode="HTML")


@dp.message(Command("stop"))
async def cmd_stop(msg: Message):
    chat_id = msg.chat.id
    if chat_id in tasks and not tasks[chat_id].done():
        tasks[chat_id].cancel()
        await msg.answer("⏹ Остановлен.")
    else:
        await msg.answer("ℹ️ Не был запущен.")


@dp.message(Command("status"))
async def cmd_status(msg: Message):
    chat_id = msg.chat.id
    if chat_id not in configs:
        await msg.answer("⚠️ Нет настроек. /setup")
        return
    cfg = configs[chat_id]
    status = "▶️ Работает" if (chat_id in tasks and not tasks[chat_id].done()) else "⏹ Остановлен"
    await msg.answer(
        f"📊 {status}\n\n🔗 {cfg['url']}\n⏱ {cfg['cooldown']} мин.\n"
        f"⭐ {cfg['min_rating']}\n👥 {cfg['max_competitors']}",
        parse_mode="HTML"
    )


async def main():
    global browser
    bot = Bot(token=BOT_TOKEN)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        log.info("Бот запущен!")
        await dp.start_polling(bot, skip_updates=True)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
