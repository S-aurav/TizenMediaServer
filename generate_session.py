#!/usr/bin/env python3
"""
Generate Telegram Session String for Deployment
Run this locally to get your session string.
"""

import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

# Replace with your credentials
API_ID = "YOUR_API_ID"  # Replace with your API ID
API_HASH = "YOUR_API_HASH"  # Replace with your API Hash

async def main():
    print("🔐 Generating Telegram Session String for Deployment")
    print("=" * 50)
    
    # Create client with string session
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    
    await client.start()
    
    # Get session string
    session_string = client.session.save()
    
    print("✅ Session string generated successfully!")
    print("📋 Copy this session string to your Render environment variables:")
    print("-" * 70)
    print(session_string)
    print("-" * 70)
    print("⚠️ Keep this session string secure and private!")
    
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
