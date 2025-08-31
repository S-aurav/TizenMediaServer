#!/usr/bin/env python3
"""
Simple Direct File Serving Implementation
Downloads files from Telegram and serves them directly without conversion
"""

import os
import asyncio
import time
from typing import Dict, List
import aiofiles
from fastapi import HTTPException
from fastapi.responses import StreamingResponse, FileResponse
import mimetypes

class DirectFileManager:
    """Manages direct file downloads and serving with storage limits"""
    
    def __init__(self, storage_dir: str, max_storage_mb: int = 1800):
        self.storage_dir = storage_dir
        self.max_storage_bytes = max_storage_mb * 1024 * 1024  # Convert to bytes
        self.file_registry: Dict[str, dict] = {}  # msg_id -> file info
        
        # Ensure storage directory exists
        os.makedirs(self.storage_dir, exist_ok=True)
        
    def get_storage_usage(self) -> int:
        """Get current storage usage in bytes"""
        total_size = 0
        if os.path.exists(self.storage_dir):
            for filename in os.listdir(self.storage_dir):
                filepath = os.path.join(self.storage_dir, filename)
                if os.path.isfile(filepath):
                    total_size += os.path.getsize(filepath)
        return total_size
    
    def get_file_list_by_age(self) -> List[tuple]:
        """Get list of files sorted by age (oldest first)"""
        files = []
        for filename in os.listdir(self.storage_dir):
            filepath = os.path.join(self.storage_dir, filename)
            if os.path.isfile(filepath):
                mtime = os.path.getmtime(filepath)
                size = os.path.getsize(filepath)
                files.append((filepath, mtime, size))
        
        # Sort by modification time (oldest first)
        files.sort(key=lambda x: x[1])
        return files
    
    async def cleanup_old_files(self, target_size: int):
        """Remove old files until storage is under target size"""
        current_size = self.get_storage_usage()
        print(f"🧹 Current storage: {current_size / 1024 / 1024:.1f} MB")
        
        if current_size <= target_size:
            return
        
        files_by_age = self.get_file_list_by_age()
        freed_space = 0
        
        for filepath, mtime, size in files_by_age:
            try:
                os.unlink(filepath)
                freed_space += size
                print(f"🗑️ Deleted old file: {os.path.basename(filepath)} ({size / 1024 / 1024:.1f} MB)")
                
                # Remove from registry
                filename = os.path.basename(filepath)
                msg_id = filename.split('_')[0] if '_' in filename else filename.split('.')[0]
                if msg_id in self.file_registry:
                    del self.file_registry[msg_id]
                
                if current_size - freed_space <= target_size:
                    break
                    
            except Exception as e:
                print(f"⚠️ Error deleting {filepath}: {e}")
        
        new_size = self.get_storage_usage()
        print(f"✅ Cleanup complete. Storage: {new_size / 1024 / 1024:.1f} MB (freed {freed_space / 1024 / 1024:.1f} MB)")
    
    async def ensure_storage_space(self, required_size: int):
        """Ensure we have enough space for new file"""
        current_size = self.get_storage_usage()
        
        if current_size + required_size > self.max_storage_bytes:
            # Need to free up space
            target_size = self.max_storage_bytes - required_size - (100 * 1024 * 1024)  # Leave 100MB buffer
            await self.cleanup_old_files(target_size)
    
    def register_file(self, msg_id: str, filepath: str, filename: str, file_size: int):
        """Register a downloaded file"""
        self.file_registry[msg_id] = {
            "filepath": filepath,
            "filename": filename,
            "size": file_size,
            "downloaded_at": time.time()
        }
    
    def get_file_info(self, msg_id: str) -> dict:
        """Get file info for a message ID"""
        return self.file_registry.get(msg_id)
    
    def file_exists(self, msg_id: str) -> bool:
        """Check if file exists for message ID"""
        if msg_id not in self.file_registry:
            return False
        
        filepath = self.file_registry[msg_id]["filepath"]
        return os.path.exists(filepath)

# Global file manager instance
direct_file_manager = DirectFileManager(
    storage_dir=os.path.join(os.getcwd(), "direct_files"),
    max_storage_mb=1800
)

async def download_and_serve_direct(msg_id: str):
    """Download file from Telegram and prepare for direct serving"""
    global client, direct_file_manager
    
    try:
        print(f"📁 Starting direct download for msg_id: {msg_id}")
        
        # Check if file already exists
        if direct_file_manager.file_exists(msg_id):
            print(f"✅ File already downloaded for {msg_id}")
            return True
        
        # Load video data to find the episode
        data = await load_video_data()
        episode_info = None
        
        for series_name, series_data in data.items():
            for season_name, episodes in series_data.items():
                for episode in episodes:
                    _, ep_msg_id = parse_telegram_url(episode["url"])
                    if ep_msg_id == int(msg_id):
                        episode_info = {
                            "url": episode["url"],
                            "title": episode.get("title", f"Episode {msg_id}"),
                            "series": series_name,
                            "season": season_name
                        }
                        break
                if episode_info:
                    break
            if episode_info:
                break
        
        if not episode_info:
            print(f"❌ Episode with msg_id {msg_id} not found")
            return False
        
        # Get Telegram message
        channel, telegram_msg_id = parse_telegram_url(episode_info["url"])
        message = await client.get_messages(channel, ids=telegram_msg_id)
        
        if not message or not message.document:
            print(f"❌ Message or document not found for {msg_id}")
            return False
        
        # Get file info
        total_size = message.document.size
        
        # Extract filename
        filename = f"{msg_id}.mkv"
        if message.document.attributes:
            for attr in message.document.attributes:
                if hasattr(attr, 'file_name') and attr.file_name:
                    filename = attr.file_name
                    break
        
        print(f"📁 Direct download: {filename} ({total_size / 1024 / 1024:.1f} MB)")
        
        # Ensure we have storage space
        await direct_file_manager.ensure_storage_space(total_size)
        
        # Download file
        filepath = os.path.join(direct_file_manager.storage_dir, f"{msg_id}_{filename}")
        
        def progress_callback(current, total):
            progress = (current / total) * 100
            if int(progress) % 10 == 0:  # Log every 10%
                print(f"📊 Download: {current / 1024 / 1024:.1f} MB / {total / 1024 / 1024:.1f} MB ({progress:.1f}%)")
        
        # Download the file
        await client.download_media(
            message,
            file=filepath,
            progress_callback=progress_callback
        )
        
        # Register the file
        direct_file_manager.register_file(msg_id, filepath, filename, total_size)
        
        print(f"✅ Direct download completed: {filepath}")
        return True
        
    except Exception as e:
        print(f"❌ Error in direct download for {msg_id}: {e}")
        import traceback
        print(f"❌ Traceback: {traceback.format_exc()}")
        return False

def get_mime_type(filename: str) -> str:
    """Get MIME type for file"""
    mime_type, _ = mimetypes.guess_type(filename)
    if mime_type:
        return mime_type
    
    # Default MIME types for common video formats
    ext = filename.lower().split('.')[-1] if '.' in filename else ''
    video_types = {
        'mkv': 'video/x-matroska',
        'mp4': 'video/mp4',
        'avi': 'video/x-msvideo',
        'mov': 'video/quicktime',
        'wmv': 'video/x-ms-wmv',
        'flv': 'video/x-flv',
        'webm': 'video/webm'
    }
    
    return video_types.get(ext, 'video/x-matroska')

async def create_streaming_response(filepath: str, filename: str, request=None):
    """Create streaming response for video file"""
    file_size = os.path.getsize(filepath)
    mime_type = get_mime_type(filename)
    
    print(f"📺 Serving direct file: {filename} ({file_size / 1024 / 1024:.1f} MB)")
    
    # For range requests (seeking support)
    range_header = request.headers.get('range') if request else None
    
    if range_header:
        # Handle range requests for seeking
        ranges = range_header.replace('bytes=', '').split('-')
        start = int(ranges[0]) if ranges[0] else 0
        end = int(ranges[1]) if ranges[1] else file_size - 1
        
        content_length = end - start + 1
        
        async def file_streamer():
            async with aiofiles.open(filepath, 'rb') as f:
                await f.seek(start)
                remaining = content_length
                chunk_size = 8192
                
                while remaining > 0:
                    chunk = await f.read(min(chunk_size, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
        
        headers = {
            'Content-Range': f'bytes {start}-{end}/{file_size}',
            'Accept-Ranges': 'bytes',
            'Content-Length': str(content_length),
            'Content-Type': mime_type,
        }
        
        return StreamingResponse(
            file_streamer(),
            status_code=206,
            headers=headers,
            media_type=mime_type
        )
    
    else:
        # Full file response
        return FileResponse(
            filepath,
            media_type=mime_type,
            filename=filename,
            headers={
                'Accept-Ranges': 'bytes',
                'Content-Length': str(file_size)
            }
        )

# Test function
async def test_direct_serving():
    """Test the direct serving functionality"""
    msg_id = "3962"
    
    print("🧪 Testing Direct File Serving")
    print("=" * 40)
    
    # Test download
    success = await download_and_serve_direct(msg_id)
    if success:
        print("✅ Download successful")
        
        # Test file info
        file_info = direct_file_manager.get_file_info(msg_id)
        if file_info:
            print(f"📋 File info: {file_info}")
            
            # Test serving
            filepath = file_info["filepath"]
            filename = file_info["filename"]
            
            if os.path.exists(filepath):
                print(f"✅ File ready for serving: {filepath}")
                print(f"📺 MIME type: {get_mime_type(filename)}")
            else:
                print(f"❌ File not found: {filepath}")
        else:
            print("❌ File info not found")
    else:
        print("❌ Download failed")

if __name__ == "__main__":
    asyncio.run(test_direct_serving())
