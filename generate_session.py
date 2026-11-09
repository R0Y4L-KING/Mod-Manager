"""
MOD MANAGER — Telethon StringSession generator
===============================================
Run this in Termux (or any PC) to create a fresh SESSION_STRING.

    pkg install python -y
    pip install telethon
    python generate_session.py

The login appears in Telegram → Settings → Devices as
"Mod-Manager", so you can recognise it and NOT log it out.

⚠️  Logging that session out from Telegram → Devices breaks the bot
    (it can no longer import the channel). If that happens, just run
    this script again and update SESSION_STRING on Render.

ℹ️  One session string works for BOTH bots (Mod-Manager and
    Update-Tracker) — no need to generate two.
"""

import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession

# Inhi naam se Telegram → Settings → Devices me dikhega
DEVICE_NAME = "Mod-Manager"
SYSTEM_VERSION = "Linux"
APP_VERSION = "1.0"


async def main():
    print("=" * 58)
    print(" MOD MANAGER — session generator")
    print("=" * 58)
    print("API_ID / API_HASH yahan se lo: https://my.telegram.org")
    print()

    api_id_raw = input("API_ID: ").strip()
    api_hash = input("API_HASH: ").strip()

    try:
        api_id = int(api_id_raw)
    except ValueError:
        print("❌ API_ID number hona chahiye (jaise 37128318).")
        return

    if not api_hash:
        print("❌ API_HASH khali hai.")
        return

    client = TelegramClient(
        StringSession(),
        api_id,
        api_hash,
        device_model=DEVICE_NAME,      # Devices list me ye naam dikhega
        system_version=SYSTEM_VERSION,
        app_version=APP_VERSION,
    )

    print()
    print("Ab phone number, OTP aur (agar hai to) 2FA password maanga jayega.")
    print("Number country code ke saath likho, jaise +9198XXXXXXXX")
    print()

    await client.start()

    me = await client.get_me()
    full_name = " ".join(filter(None, [getattr(me, "first_name", None),
                                       getattr(me, "last_name", None)]))
    print()
    print(f"✅ Login successful — {full_name or 'account'} "
          f"(@{me.username or '—'})")
    print(f"📱 Telegram → Settings → Devices me dikhega: \"{DEVICE_NAME}\"")
    print()
    print("=" * 58)
    print(" SESSION_STRING (copy karke Render me daalo)")
    print("=" * 58)
    print()
    print(client.session.save())
    print()
    print("=" * 58)
    print("Aage ke steps:")
    print(" 1. Render → Mod-Manager service → Environment")
    print(" 2. SESSION_STRING = (upar wali lambi string, poori copy karo)")
    print(" 3. Save → Manual Deploy → Deploy latest commit")
    print(" 4. 30-60 sec baad /start chalao — count 0 nahi hona chahiye")
    print()
    print("⚠️  Telegram → Devices me 'Mod-Manager' ko LOGOUT mat karna,")
    print("    warna bot channel import nahi kar payega.")
    print("=" * 58)

    await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
