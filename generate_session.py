"""
Telethon StringSession generator.
Run locally:  python generate_session.py
Phir output ko Render ke SESSION_STRING env var me daalo.
"""

import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = int(input("API_ID (my.telegram.org se): "))
API_HASH = input("API_HASH: ")


async def main():
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.start()
    print("\n✅ Login successful!\n")
    print("SESSION_STRING (Render env var me daalo):\n")
    print(client.session.save())
    print("\n⚠️ Isko kisi ke saath share mat karna!")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
