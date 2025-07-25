# filepath: c:\Users\yadav\OneDrive\Desktop\python Bot\queue_manager.py
import asyncio
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from enum import Enum

class DownloadPriority(Enum):
    HIGH = 1  # Single episode downloads
    LOW = 2   # Season downloads

@dataclass
class DownloadTask:
    msg_id: int
    channel: Any
    message: Any
    filename: str
    priority: DownloadPriority
    created_at: float
    series_name: Optional[str] = None
    season_name: Optional[str] = None
    episode_title: Optional[str] = None

class QueueManager:
    def __init__(self, max_low_priority_downloads: int = 3, max_high_priority_downloads: int = 1):
        self.max_low_priority_downloads = max_low_priority_downloads
        self.max_high_priority_downloads = max_high_priority_downloads
        self.max_total_downloads = max_low_priority_downloads + max_high_priority_downloads
        
        # Two priority queues
        self.high_priority_queue: List[DownloadTask] = []  # Single episodes
        self.low_priority_queue: List[DownloadTask] = []   # Season episodes
        
        # Active download slots with priority tracking
        self.active_downloads: Dict[int, asyncio.Task] = {}  # slot_id -> task
        self.slot_priorities: Dict[int, DownloadPriority] = {}  # slot_id -> priority
        self.slot_tasks: Dict[int, DownloadTask] = {}  # slot_id -> task_info
        self.download_tasks: Dict[int, DownloadTask] = {}    # msg_id -> task_info
        
        # Slot allocation: 0-2 = LOW priority slots, 3 = HIGH priority slot
        self.low_priority_slots = list(range(max_low_priority_downloads))  # [0, 1, 2]
        self.high_priority_slots = list(range(max_low_priority_downloads, self.max_total_downloads))  # [3]
        
        # Upload queue (sequential)
        self.upload_queue: List[str] = []  # List of file_paths waiting to upload
        self.upload_in_progress: bool = False
        
        # Statistics
        self.stats = {
            "total_queued": 0,
            "total_completed": 0,
            "total_failed": 0,
            "high_priority_completed": 0,
            "low_priority_completed": 0
        }
        
        print(f"🎛️ Queue Manager initialized:")
        print(f"   📋 LOW priority slots: {self.low_priority_slots} (max {max_low_priority_downloads})")
        print(f"   🔥 HIGH priority slots: {self.high_priority_slots} (max {max_high_priority_downloads})")
        print(f"   📊 Total slots: {self.max_total_downloads}")

    def add_download_task(self, task: DownloadTask) -> bool:
        """Add a download task to appropriate priority queue"""
        # Check if already in queue or downloading
        if task.msg_id in self.download_tasks:
            return False
        
        # Add to appropriate queue
        if task.priority == DownloadPriority.HIGH:
            self.high_priority_queue.append(task)
            if task.series_name and task.season_name:
                print(f"🔥 HIGH priority queued: {task.series_name} - {task.season_name}")
                print(f"   🎬 Episode: {task.episode_title or task.filename}")
            else:
                print(f"🔥 HIGH priority queued: {task.filename}")
            print(f"   📊 Queue status: H:{len(self.high_priority_queue)}, L:{len(self.low_priority_queue)}")
        else:
            self.low_priority_queue.append(task)
            if task.series_name and task.season_name:
                print(f"📋 LOW priority queued: {task.series_name} - {task.season_name}")
                print(f"   🎬 Episode: {task.episode_title or task.filename}")
            else:
                print(f"📋 LOW priority queued: {task.filename}")
            print(f"   📊 Queue status: H:{len(self.high_priority_queue)}, L:{len(self.low_priority_queue)}")
        
        self.download_tasks[task.msg_id] = task
        self.stats["total_queued"] += 1
        
        return True

    def get_next_task(self) -> Optional[DownloadTask]:
        """Get next task following priority rules (High priority first, then FCFS within each level)"""
        # Always prioritize high priority queue
        if self.high_priority_queue:
            task = self.high_priority_queue.pop(0)  # FCFS
            return task
        
        # If no high priority, check low priority
        if self.low_priority_queue:
            task = self.low_priority_queue.pop(0)  # FCFS
            return task
        
        return None

    def get_available_slot(self) -> Optional[int]:
        """Get next available download slot based on priority rules"""
        # Check for available high priority slot first (for high priority tasks)
        for slot_id in self.high_priority_slots:
            if slot_id not in self.active_downloads:
                return slot_id
        
        # Check for available low priority slots
        for slot_id in self.low_priority_slots:
            if slot_id not in self.active_downloads:
                return slot_id
        
        return None

    def get_available_slot_for_priority(self, priority: DownloadPriority) -> Optional[int]:
        """Get available slot for specific priority level"""
        if priority == DownloadPriority.HIGH:
            # High priority gets dedicated high priority slots first
            for slot_id in self.high_priority_slots:
                if slot_id not in self.active_downloads:
                    return slot_id
            # If high priority slots full, can use low priority slots if available
            for slot_id in self.low_priority_slots:
                if slot_id not in self.active_downloads:
                    return slot_id
        else:
            # Low priority only gets low priority slots
            for slot_id in self.low_priority_slots:
                if slot_id not in self.active_downloads:
                    return slot_id
        
        return None

    def start_download_slot(self, slot_id: int, task: DownloadTask, download_coro) -> bool:
        """Start a download in the specified slot"""
        if slot_id in self.active_downloads:
            return False
        
        print(f"🎯 [Slot {slot_id}] Starting {task.priority.name}: {task.filename}")
        if task.series_name and task.season_name:
            print(f"   📺 Series: {task.series_name} - Season: {task.season_name}")
            if task.episode_title:
                print(f"   🎬 Episode: {task.episode_title}")
        
        self.active_downloads[slot_id] = asyncio.create_task(download_coro)
        self.slot_priorities[slot_id] = task.priority
        self.slot_tasks[slot_id] = task  # Track which task is in which slot
        
        # Log current queue status after starting download
        self.log_detailed_status()
        
        return True

    def complete_download_slot(self, slot_id: int, msg_id: int, success: bool):
        """Mark a download slot as completed"""
        if slot_id in self.active_downloads:
            del self.active_downloads[slot_id]
        
        if slot_id in self.slot_priorities:
            del self.slot_priorities[slot_id]
        
        if slot_id in self.slot_tasks:
            del self.slot_tasks[slot_id]
        
        if msg_id in self.download_tasks:
            task = self.download_tasks[msg_id]
            if success:
                self.stats["total_completed"] += 1
                if task.priority == DownloadPriority.HIGH:
                    self.stats["high_priority_completed"] += 1
                    print(f"✅ [Slot {slot_id}] HIGH priority completed: {task.filename}")
                else:
                    self.stats["low_priority_completed"] += 1
                    print(f"✅ [Slot {slot_id}] LOW priority completed: {task.filename}")
            else:
                self.stats["total_failed"] += 1
                print(f"❌ [Slot {slot_id}] Failed: {task.filename}")
            
            del self.download_tasks[msg_id]
            
            # Log status after completion
            self.log_detailed_status()

    def get_queue_status(self) -> Dict:
        """Get current queue status with detailed task information"""
        # Get detailed information about active downloads
        active_downloads_detail = {}
        for slot_id, priority in self.slot_priorities.items():
            # Find the task for this slot
            task_detail = None
            for msg_id, task in self.download_tasks.items():
                # Check if this task is currently running (not just queued)
                if msg_id in [t.msg_id for t in self.download_tasks.values() if t.msg_id in self.download_tasks]:
                    # This is a bit complex - let's track active tasks differently
                    pass
            
            active_downloads_detail[str(slot_id)] = {
                "priority": priority.name,
                "slot_type": "HIGH" if slot_id in self.high_priority_slots else "LOW"
            }
        
        # Get detailed queue information
        high_priority_queue_detail = []
        for task in self.high_priority_queue:
            detail = {
                "msg_id": task.msg_id,
                "filename": task.filename,
                "series": task.series_name or "Unknown",
                "season": task.season_name or "Unknown", 
                "episode": task.episode_title or f"Episode {task.msg_id}",
                "queued_at": task.created_at
            }
            high_priority_queue_detail.append(detail)
        
        low_priority_queue_detail = []
        for task in self.low_priority_queue:
            detail = {
                "msg_id": task.msg_id,
                "filename": task.filename,
                "series": task.series_name or "Unknown",
                "season": task.season_name or "Unknown",
                "episode": task.episode_title or f"Episode {task.msg_id}",
                "queued_at": task.created_at
            }
            low_priority_queue_detail.append(detail)
        
        return {
            "high_priority_queue": len(self.high_priority_queue),
            "low_priority_queue": len(self.low_priority_queue),
            "active_downloads": len(self.active_downloads),
            "available_slots": self.max_total_downloads - len(self.active_downloads),
            "active_high_priority": len([slot for slot, priority in self.slot_priorities.items() if priority == DownloadPriority.HIGH]),
            "active_low_priority": len([slot for slot, priority in self.slot_priorities.items() if priority == DownloadPriority.LOW]),
            "total_queued": self.stats["total_queued"],
            "total_completed": self.stats["total_completed"],
            "total_failed": self.stats["total_failed"],
            "high_priority_completed": self.stats["high_priority_completed"],
            "low_priority_completed": self.stats["low_priority_completed"],
            "upload_in_progress": self.upload_in_progress,
            "upload_queue_size": len(self.upload_queue),
            "slot_allocation": {
                "low_priority_slots": self.low_priority_slots,
                "high_priority_slots": self.high_priority_slots,
                "active_slots": list(self.active_downloads.keys()),
                "slot_priorities": {str(slot): priority.name for slot, priority in self.slot_priorities.items()}
            },
            "detailed_status": {
                "active_downloads": active_downloads_detail,
                "high_priority_queue_detail": high_priority_queue_detail,
                "low_priority_queue_detail": low_priority_queue_detail
            }
        }

    def is_download_in_progress(self, msg_id: int) -> bool:
        """Check if a specific download is in progress"""
        return msg_id in self.download_tasks

    def cancel_download(self, msg_id: int) -> bool:
        """Cancel a specific download"""
        if msg_id not in self.download_tasks:
            return False
        
        # Find and cancel the task
        for slot_id, task in self.active_downloads.items():
            if self.download_tasks[msg_id].msg_id == msg_id:
                task.cancel()
                self.complete_download_slot(slot_id, msg_id, False)
                print(f"🚫 Cancelled download: {msg_id}")
                return True
        
        # If not active, remove from queue
        task_info = self.download_tasks[msg_id]
        if task_info.priority == DownloadPriority.HIGH:
            self.high_priority_queue = [t for t in self.high_priority_queue if t.msg_id != msg_id]
        else:
            self.low_priority_queue = [t for t in self.low_priority_queue if t.msg_id != msg_id]
        
        del self.download_tasks[msg_id]
        print(f"🚫 Removed from queue: {msg_id}")
        return True

    def log_detailed_status(self):
        """Log detailed status showing what's downloading and what's in queue"""
        print("\n" + "="*80)
        print("📊 DETAILED QUEUE STATUS")
        print("="*80)
        
        # Show active downloads
        if self.active_downloads:
            print("🔄 CURRENTLY DOWNLOADING:")
            for slot_id in sorted(self.active_downloads.keys()):
                priority = self.slot_priorities.get(slot_id, "UNKNOWN")
                slot_type = "HIGH" if slot_id in self.high_priority_slots else "LOW"
                
                # Get the exact task for this slot
                task_info = self.slot_tasks.get(slot_id)
                
                if task_info:
                    if task_info.series_name and task_info.season_name:
                        print(f"   [Slot {slot_id}] {priority.name} Priority ({slot_type} slot)")
                        print(f"      📺 {task_info.series_name} - {task_info.season_name}")
                        print(f"      🎬 {task_info.episode_title or task_info.filename}")
                    else:
                        print(f"   [Slot {slot_id}] {priority.name} Priority ({slot_type} slot): {task_info.filename}")
                else:
                    print(f"   [Slot {slot_id}] {priority.name} Priority ({slot_type} slot): Unknown task")
        else:
            print("🔄 CURRENTLY DOWNLOADING: None")
        
        # Show HIGH priority queue
        if self.high_priority_queue:
            print(f"\n🔥 HIGH PRIORITY QUEUE ({len(self.high_priority_queue)} episodes):")
            for i, task in enumerate(self.high_priority_queue[:5], 1):  # Show first 5
                if task.series_name and task.season_name:
                    print(f"   {i}. 📺 {task.series_name} - {task.season_name}")
                    print(f"      🎬 {task.episode_title or task.filename}")
                else:
                    print(f"   {i}. {task.filename}")
            if len(self.high_priority_queue) > 5:
                print(f"   ... and {len(self.high_priority_queue) - 5} more episodes")
        else:
            print("\n🔥 HIGH PRIORITY QUEUE: Empty")
        
        # Show LOW priority queue (grouped by series/season)
        if self.low_priority_queue:
            print(f"\n📋 LOW PRIORITY QUEUE ({len(self.low_priority_queue)} episodes):")
            
            # Group by series and season
            series_groups = {}
            for task in self.low_priority_queue:
                series_key = f"{task.series_name or 'Unknown'} - {task.season_name or 'Unknown'}"
                if series_key not in series_groups:
                    series_groups[series_key] = []
                series_groups[series_key].append(task)
            
            for series_season, tasks in series_groups.items():
                print(f"   📺 {series_season} ({len(tasks)} episodes)")
                for task in tasks[:3]:  # Show first 3 episodes per series/season
                    print(f"      • {task.episode_title or task.filename}")
                if len(tasks) > 3:
                    print(f"      • ... and {len(tasks) - 3} more episodes")
        else:
            print("\n📋 LOW PRIORITY QUEUE: Empty")
        
        # Show summary statistics
        print(f"\n📈 STATISTICS:")
        print(f"   ✅ Completed: {self.stats['total_completed']} (H:{self.stats['high_priority_completed']}, L:{self.stats['low_priority_completed']})")
        print(f"   ❌ Failed: {self.stats['total_failed']}")
        print(f"   🎛️ Available Slots: {self.max_total_downloads - len(self.active_downloads)}/{self.max_total_downloads}")
        print("="*80 + "\n")

# Global queue manager instance
queue_manager = QueueManager(max_low_priority_downloads=3, max_high_priority_downloads=1)