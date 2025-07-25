# filepath: c:\Users\yadav\OneDrive\Desktop\python Bot\utils.py
import re
import os

def parse_telegram_url(url):
    """Parse Telegram URL and extract channel and message ID"""
    public_match = re.match(r"https://t\.me/([^/]+)/(\d+)", url)
    private_match = re.match(r"https://t\.me/c/(\d+)/(\d+)", url)
    if public_match:
        return public_match.group(1), int(public_match.group(2))
    elif private_match:
        return int(private_match.group(1)), int(private_match.group(2))
    return None, None

def get_file_extension_from_message(message):
    """Extract file extension from Telegram message"""
    original_filename = None
    
    if message.video and message.video.attributes:
        for attr in message.video.attributes:
            if hasattr(attr, 'file_name') and attr.file_name:
                original_filename = attr.file_name
                break
    elif message.document and message.document.attributes:
        for attr in message.document.attributes:
            if hasattr(attr, 'file_name') and attr.file_name:
                original_filename = attr.file_name
                break
    
    return os.path.splitext(original_filename)[1] if original_filename else '.mkv'

def get_file_size_from_message(message):
    """Get file size from Telegram message"""
    if message.video:
        return message.video.size
    elif message.document:
        return message.document.size
    else:
        return 0