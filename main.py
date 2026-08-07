import asyncio

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import BOT_TOKEN, OWNER_ID, CHANNEL_ID

from parser import extract_app_name, extract_expiry

from database import (
    add_app,
    add_update,
    get_update
)

from scheduler import expiry_checker



bot = Bot(BOT_TOKEN)

dp = Dispatcher()



# ==========================
# CHANNEL POST READER
# ==========================

@dp.channel_post()
async def channel_reader(message: types.Message):

    text = (
        message.caption
        or message.text
        or ""
    )


    expiry = extract_expiry(text)


    if expiry:

        app = extract_app_name(text)


        if app:

            add_app(
                app,
                message.chat.id,
                message.message_id,
                expiry
            )


            await bot.send_message(
                OWNER_ID,

                f"""
✅ New App Added

📱 #{app}

📅 Expiry:
{expiry}
"""
            )



# ==========================
# SAVE UPDATE POST
# ==========================

@dp.message()
async def save_update(message: types.Message):

    if message.from_user.id != OWNER_ID:
        return


    text = (
        message.caption
        or message.text
        or ""
    )


    app = extract_app_name(text)


    if app:

        add_update(
            app,
            message.message_id
        )


        await message.reply(
            f"""
✅ Update Saved

📱 #{app}

Expiry hone par use kiya jayega.
"""
        )



# ==========================
# START COMMAND
# ==========================

@dp.message(Command("start"))
async def start(message: types.Message):

    if message.from_user.id != OWNER_ID:
        return


    await message.answer(
        """
🤖 App Expiry Manager Bot

Status: Active ✅

Channel monitoring ON.
"""
    )



# ==========================
# BUTTON HANDLER
# ==========================

@dp.callback_query()
async def button_handler(
    call: types.CallbackQuery
):

    data = call.data


    if data.startswith("repost:"):

        app = data.split(":")[1]


        update_id = get_update(app)


        if update_id == 0:

            await call.message.answer(
                "❌ No update saved"
            )

            return



        await bot.copy_message(
            chat_id=CHANNEL_ID,

            from_chat_id=OWNER_ID,

            message_id=update_id
        )


        await call.message.answer(
            "✅ Update Posted Successfully"
        )



    elif data == "ignore":

        await call.message.answer(
            "❌ Cancelled"
        )



# ==========================
# BOT START
# ==========================

async def main():

    scheduler = AsyncIOScheduler()


    scheduler.add_job(
        expiry_checker,

        "interval",

        hours=24,

        args=[bot]
    )


    scheduler.start()


    print("Bot Started...")


    await dp.start_polling(bot)



if __name__ == "__main__":

    asyncio.run(main())