from datetime import datetime, timedelta

from database import get_expiring
from config import OWNER_ID



async def expiry_checker(bot):

    today = datetime.now().strftime("%d/%m/%Y")


    apps = get_expiring(today)


    for app in apps:

        app_name = app[1]


        await bot.send_message(
            OWNER_ID,

            f"""
⚠️ APP EXPIRED

📱 App:
#{app_name}

Old post expire ho gayi hai.

Kya update repost karna hai?
"""
        )