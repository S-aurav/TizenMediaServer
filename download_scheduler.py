# filepath: c:\Users\yadav\OneDrive\Desktop\python Bot\download_scheduler.py
import asyncio
import time
from queue_manager import queue_manager, DownloadTask, DownloadPriority
from download_manager import chunked_download_upload
from upload_manager import upload_to_pixeldrain
from database_manager import load_uploaded_files_db, save_uploaded_files_db
from utils import parse_telegram_url, get_file_extension_from_message, get_file_size_from_message

class DownloadScheduler:
    def __init__(self):
        self.scheduler_running = False
        self.scheduler_task = None
        self.client = None  # Store client reference
        
    async def start_scheduler(self, client=None):
        """Start the download scheduler"""
        if self.scheduler_running:
            return
        
        if client:
            self.client = client
        
        self.scheduler_running = True
        self.scheduler_task = asyncio.create_task(self._scheduler_loop())
        print("🚀 Download scheduler started")
    
    async def stop_scheduler(self):
        """Stop the download scheduler"""
        if not self.scheduler_running:
            return
        
        self.scheduler_running = False
        if self.scheduler_task:
            self.scheduler_task.cancel()
        print("🛑 Download scheduler stopped")
    
    async def _scheduler_loop(self):
        """Main scheduler loop - processes queues and manages download slots"""
        while self.scheduler_running:
            try:
                # Process high priority queue first
                if queue_manager.high_priority_queue:
                    # Get available slot for high priority
                    available_slot = queue_manager.get_available_slot_for_priority(DownloadPriority.HIGH)
                    if available_slot is not None:
                        next_task = queue_manager.get_next_task()  # This will return high priority task
                        if next_task and next_task.priority == DownloadPriority.HIGH:
                            # Start high priority download
                            download_coro = self._execute_download(available_slot, next_task)
                            queue_manager.start_download_slot(available_slot, next_task, download_coro)
                            continue  # Check for more high priority tasks immediately
                
                # Process low priority queue if no high priority or no slots for high priority
                if queue_manager.low_priority_queue:
                    # Get available slot for low priority
                    available_slot = queue_manager.get_available_slot_for_priority(DownloadPriority.LOW)
                    if available_slot is not None:
                        # Make sure we get a low priority task
                        if queue_manager.low_priority_queue:
                            next_task = queue_manager.low_priority_queue.pop(0)
                            queue_manager.download_tasks[next_task.msg_id] = next_task
                            # Start low priority download
                            download_coro = self._execute_download(available_slot, next_task)
                            queue_manager.start_download_slot(available_slot, next_task, download_coro)
                            continue
                
                # No available slots or no tasks, wait a bit
                await asyncio.sleep(2)
                
            except Exception as e:
                print(f"❌ Error in scheduler loop: {e}")
                await asyncio.sleep(5)
    
    async def _execute_download(self, slot_id: int, task: DownloadTask):
        """Execute a download task in a specific slot using just-in-time message fetching"""
        success = False
        
        try:
            # Check if already uploaded
            uploaded_files = load_uploaded_files_db()
            if str(task.msg_id) in uploaded_files:
                file_info = uploaded_files[str(task.msg_id)]
                # Verify file still exists
                from upload_manager import check_pixeldrain_file_exists
                if await check_pixeldrain_file_exists(file_info["pixeldrain_id"]):
                    success = True
                    return
            
            # Use just-in-time message fetching to avoid file reference expiration
            from download_manager import get_fresh_message_and_download
            from upload_manager import upload_to_pixeldrain
            
            pixeldrain_id = await get_fresh_message_and_download(
                self.client,  # Use scheduler's client reference
                task.channel,
                task.msg_id,
                task.filename,
                upload_to_pixeldrain
            )
            
            # Save to database
            uploaded_files = load_uploaded_files_db()
            file_size = 0  # We'll get this from the fresh message
            
            uploaded_files[str(task.msg_id)] = {
                "pixeldrain_id": pixeldrain_id,
                "filename": task.filename,
                "uploaded_at": int(time.time()),
                "file_size": file_size,
                "access_count": 0
            }
            save_uploaded_files_db(uploaded_files)
            
            success = True
            
        except Exception as e:
            print(f"❌ [Slot {slot_id}] Download failed: {task.filename} - {e}")
        
        finally:
            # Mark slot as completed
            queue_manager.complete_download_slot(slot_id, task.msg_id, success)
    
    async def queue_single_episode_download(self, url: str, client) -> dict:
        """Queue a single episode download (HIGH priority)"""
        channel, msg_id = parse_telegram_url(url)
        if not channel or not msg_id:
            raise ValueError("Invalid Telegram URL")
        
        # Check if already in queue
        if queue_manager.is_download_in_progress(msg_id):
            return {"status": "already_queued", "msg_id": msg_id}
        
        # Check if already uploaded
        uploaded_files = load_uploaded_files_db()
        if str(msg_id) in uploaded_files:
            file_info = uploaded_files[str(msg_id)]
            from upload_manager import check_pixeldrain_file_exists
            if await check_pixeldrain_file_exists(file_info["pixeldrain_id"]):
                return {
                    "status": "already_uploaded",
                    "pixeldrain_id": file_info["pixeldrain_id"],
                    "msg_id": msg_id
                }
        
        # Get message info for filename (but don't store the message object)
        if not client.is_connected():
            await client.connect()
        
        message = await client.get_messages(channel, ids=msg_id)
        if not message or (not message.video and not message.document):
            raise ValueError("No video or document found")
        
        # Create download task (WITHOUT storing message object)
        file_ext = get_file_extension_from_message(message)
        filename = f"{msg_id}{file_ext}"
        
        task = DownloadTask(
            msg_id=msg_id,
            channel=channel,  # Store channel, not message
            filename=filename,
            priority=DownloadPriority.HIGH,  # Single episodes are HIGH priority
            created_at=time.time()
        )
        
        # Add to queue
        if queue_manager.add_download_task(task):
            # Start scheduler if not running
            if not self.scheduler_running:
                await self.start_scheduler(client)
            
            return {
                "status": "queued",
                "msg_id": msg_id,
                "priority": "HIGH",
                "queue_position": len(queue_manager.high_priority_queue)
            }
        else:
            return {"status": "queue_failed", "msg_id": msg_id}
    
    async def queue_season_episode_download(self, episode_data: dict, client) -> bool:
        """Queue a season episode download (LOW priority)"""
        channel, msg_id = parse_telegram_url(episode_data["url"])
        if not channel or not msg_id:
            return False
        
        # Check if already in queue
        if queue_manager.is_download_in_progress(msg_id):
            return True  # Consider it successful if already queued
        
        # Check if already uploaded
        uploaded_files = load_uploaded_files_db()
        if str(msg_id) in uploaded_files:
            file_info = uploaded_files[str(msg_id)]
            from upload_manager import check_pixeldrain_file_exists
            if await check_pixeldrain_file_exists(file_info["pixeldrain_id"]):
                return True  # Already exists
        
        # Get message info for filename (but don't store the message object)
        if not client.is_connected():
            await client.connect()
        
        message = await client.get_messages(channel, ids=msg_id)
        if not message or (not message.video and not message.document):
            return False
        
        # Create download task (WITHOUT storing message object)
        file_ext = get_file_extension_from_message(message)
        filename = f"{msg_id}{file_ext}"
        
        task = DownloadTask(
            msg_id=msg_id,
            channel=channel,  # Store channel, not message
            filename=filename,
            priority=DownloadPriority.LOW,  # Season episodes are LOW priority
            created_at=time.time(),
            series_name=episode_data.get("series_name"),
            season_name=episode_data.get("season_name"),
            episode_title=episode_data.get("title")
        )
        
        # Add to queue
        success = queue_manager.add_download_task(task)
        
        # Start scheduler if not running
        if success and not self.scheduler_running:
            await self.start_scheduler(client)
        
        return success
    
    def get_download_status(self) -> dict:
        """Get current download status"""
        return queue_manager.get_queue_status()

# Global download scheduler instance
download_scheduler = DownloadScheduler()