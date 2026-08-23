import asyncio
import logging
import re
import os
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

BOT_TOKEN = os.getenv("8657083122:AAFfn-iGiiVKMYsBVkrWHBqZk0hNchHNmrY")
PLAYEROK_TOKEN = os.getenv("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxZjE5ZjJlNS0wNTY2LTZmYjAtZDhlOS01MWFhODFlNGJkOTkiLCJpZGVudGl0eSI6IjFmMTlmMmU1LTA1ODQtNjQ3MC02Mjk1LWVmOWNiZjc4MTg1ZCIsInJvbGUiOiJVU0VSIiwidiI6MSwicmV2IjoxLCJpYXQiOjE3ODc1MTU4NTksImV4cCI6MTgxOTA1MTg1OX0.wY5PnQq5V_TFhxRKaYqSyA5oUa8t9B8M8FqIEPtbQlU")
PLAYEROK_DDG3 = os.getenv("89.149.226.17")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger(__name__)


class SetupStates(StatesGroup):
    waiting_url = State()
    waiting_cooldown = State()
    waiting_min_rating = State()
    waiting_max_competitors = State()


tasks: dict[int, asyncio.Task] = {}
configs: dict[int, dict] = {}

browser: Optional[Browser] = None
context = None


async def fetch_with_playwright(url: str) -> Optional[str]:
    global context

    try:
        page = await context.new_page()

        await page.goto(
            url,
            wait_until="networkidle",
            timeout=30000
        )

        html = await page.content()

        await page.close()

        return html

    except Exception as e:
        log.error(f"Playwright ошибка: {e}")
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


            title = link.get_text(strip=True)

            if not title:
                continue


            price = 0

            nums = re.findall(r"\d+", title)

            if nums:
                price = int(nums[0])


            lots.append({
                "title": title,
                "price": price,
                "url": href,
                "seller_rating": 0
            })

        except Exception:
            pass


    return lots
def analyze_competition(lots: list[dict]) -> list[dict]:

    def normalize(title: str) -> str:
        t = re.sub(
            r"\d+\s*(руб|₽|rub|\$|usd|coins?)",
            "",
            title,
            flags=re.IGNORECASE
        )

        t = re.sub(r"\d+", "", t)
        t = re.sub(r"\s+", " ", t)

        return t.lower().strip()


    normalized = [
        normalize(lot["title"])
        for lot in lots
    ]

    counts = Counter(normalized)

    result = []
    seen = set()


    for lot, norm in zip(lots, normalized):

        if norm in seen:
            continue

        seen.add(norm)

        copy_lot = dict(lot)
        copy_lot["competitors"] = counts[norm] - 1

        result.append(copy_lot)


    return result



async def parse_and_notify(
        bot: Bot,
        chat_id: int,
        cfg: dict
):

    url = cfg["url"]
    cooldown = cfg["cooldown"]
    min_rating = cfg["min_rating"]
    max_competitors = cfg["max_competitors"]

    known_urls = set()


    while True:

        try:

            html = await fetch_with_playwright(url)


            if not html:
                await asyncio.sleep(cooldown * 60)
                continue


            lots = parse_lots(html)


            if not lots:

                await bot.send_message(
                    chat_id,
                    "⚠️ Лоты не найдены"
                )

                await asyncio.sleep(cooldown * 60)
                continue



            analyzed = analyze_competition(lots)


            good_lots = [
                lot for lot in analyzed

                if lot["seller_rating"] >= min_rating
                and lot["competitors"] <= max_competitors
                and lot["url"] not in known_urls
            ]



            for lot in good_lots[:10]:

                known_urls.add(lot["url"])


                text = (
                    "🎯 <b>Найден лот!</b>\n\n"
                    f"📦 {lot['title']}\n"
                    f"💰 Цена: {lot['price']} ₽\n"
                    f"👥 Конкурентов: {lot['competitors']}\n"
                    f"🔗 {lot['url']}"
                )


                await bot.send_message(
                    chat_id,
                    text,
                    parse_mode="HTML"
                )


            await asyncio.sleep(
                cooldown * 60
            )


        except asyncio.CancelledError:
            break


        except Exception as e:

            log.error(
                f"Ошибка {chat_id}: {e}"
            )

            await asyncio.sleep(30)



dp = Dispatcher(
    storage=MemoryStorage()
)



@dp.message(Command("start"))
async def start(
        msg: Message,
        state: FSMContext
):

    await state.clear()

    await msg.answer(
        "👋 Playerok Parser Bot\n\n"
        "/setup - настройка\n"
        "/start_parse - запуск\n"
        "/stop - остановить\n"
        "/status - статус"
    )



@dp.message(Command("setup"))
async def setup(
        msg: Message,
        state: FSMContext
):

    await state.set_state(
        SetupStates.waiting_url
    )

    await msg.answer(
        "Отправь ссылку Playerok"
    )
@dp.message(SetupStates.waiting_url)
async def get_url(msg: Message, state: FSMContext):
    url = msg.text.strip()

    if not url.startswith("http"):
        await msg.answer("❌ Нужна ссылка https://")
        return

    await state.update_data(url=url)

    await state.set_state(
        SetupStates.waiting_cooldown
    )

    await msg.answer(
        "⏱ Через сколько минут проверять?"
    )
@dp.message(SetupStates.waiting_cooldown)
async def get_cooldown(
        msg: Message,
        state: FSMContext
):

    try:
        cooldown = int(msg.text)

    except:
        await msg.answer(
            "Введите число"
        )
        return


    await state.update_data(
        cooldown=cooldown
    )

    await state.set_state(
        SetupStates.waiting_min_rating
    )

    await msg.answer(
        "⭐ Минимальный рейтинг продавца"
    )



@dp.message(SetupStates.waiting_min_rating)
async def get_rating(
        msg: Message,
        state: FSMContext
):

        try:
        rating = float(msg.text.replace(",", "."))
        if rating < 0 or rating > 5:
            raise ValueError

    except ValueError:
        await msg.answer(
            "❌ Введите число от 0 до 5"
        )
        return


    await state.update_data(
        min_rating=rating
    )


    await state.set_state(
        SetupStates.waiting_max_competitors
    )


    await msg.answer(
        "👥 Максимум конкурентов"
    )



@dp.message(SetupStates.waiting_max_competitors)
async def get_competitors(
        msg: Message,
        state: FSMContext
):

    try:
        max_c = int(msg.text)

    except:
        await msg.answer(
            "Введите число"
        )
        return



    data = await state.get_data()


    configs[msg.chat.id] = {
        **data,
        "max_competitors": max_c
    }


    await state.clear()


    await msg.answer(
        "✅ Настройки сохранены\n\n"
        "Запуск: /start_parse"
    )



@dp.message(Command("start_parse"))
async def start_parse(
        msg: Message
):

    chat_id = msg.chat.id


    if chat_id not in configs:

        await msg.answer(
            "Сначала /setup"
        )

        return



    if chat_id in tasks:

        await msg.answer(
            "Уже работает"
        )

        return



    tasks[chat_id] = asyncio.create_task(
        parse_and_notify(
            msg.bot,
            chat_id,
            configs[chat_id]
        )
    )


    await msg.answer(
        "🚀 Парсер запущен"
    )



@dp.message(Command("stop"))
async def stop(
        msg: Message
):

    chat_id = msg.chat.id


    if chat_id in tasks:

        tasks[chat_id].cancel()

        del tasks[chat_id]


        await msg.answer(
            "⏹ Остановлен"
        )

    else:

        await msg.answer(
            "Не запущен"
        )



@dp.message(Command("status"))
async def status(
        msg: Message
):

    chat_id = msg.chat.id


    if chat_id in tasks:

        await msg.answer(
            "🟢 Работает"
        )

    else:

        await msg.answer(
            "🔴 Остановлен"
        )




async def main():

    global browser
    global context


    async with async_playwright() as p:


        browser = await p.chromium.launch(
            headless=True
        )


        context = await browser.new_context()


        await context.add_cookies([

            {
                "name": "token",
                "value": PLAYEROK_TOKEN,
                "domain": ".playerok.com",
                "path": "/"
            },

            {
                "name": "__ddg3",
                "value": PLAYEROK_DDG3,
                "domain": ".playerok.com",
                "path": "/"
            }

        ])


        log.info(
            "Playerok cookies загружены"
        )


        bot = Bot(
            token=BOT_TOKEN
        )


        await dp.start_polling(
            bot
        )



if __name__ == "__main__":

    asyncio.run(
        main()
    )
