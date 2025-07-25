# filepath: c:\Users\yadav\OneDrive\Desktop\python Bot\upload_manager.py
import os
import time
import asyncio
import aiohttp
import requests
import urllib3
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configuration variables (will be imported from main)
PIXELDRAIN_API_KEY = os.getenv("PIXELDRAIN_API_KEY")
PIXELDRAIN_UPLOAD_URL = "https://pixeldrain.com/api/file"

# Debug: Print API key status on import
if PIXELDRAIN_API_KEY:
    print(f"🔑 PixelDrain API Key loaded: {PIXELDRAIN_API_KEY[:8]}...{PIXELDRAIN_API_KEY[-4:]}")
else:
    print("⚠️ No PixelDrain API Key found")

async def upload_to_pixeldrain(file_path: str, filename: str) -> str:
    """Main upload function - using memory-safe streaming upload"""
    max_retries = 3
    retry_delay = 5
    
    for attempt in range(max_retries):
        try:
            print(f"🚀 Upload attempt {attempt + 1}/{max_retries} to PixelDrain: {filename}")
            
            # Use memory-safe streaming upload
            return await memory_safe_upload_async(file_path, filename)
                
        except Exception as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
                retry_delay *= 2
                continue
            else:
                raise Exception(f"Upload failed after {max_retries} attempts: {e}")

async def memory_safe_upload_async(file_path: str, filename: str) -> str:
    """Memory-safe upload using generator-based streaming"""
    import asyncio
    import functools
    
    # Run the blocking upload in a thread pool
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, functools.partial(memory_safe_upload_sync, file_path, filename))
    return result

def memory_safe_upload_sync(file_path: str, filename: str) -> str:
    """TRUE memory-safe upload using generator-based streaming (8MB max memory)"""
    urllib3.disable_warnings()
    
    # Add authentication
    auth = None
    if PIXELDRAIN_API_KEY:
        auth = ('', PIXELDRAIN_API_KEY)
        print("🔑 Using API key authentication")
    else:
        print("⚠️ No API key - using anonymous upload (limited to 5GB/day)")
    
    file_size = os.path.getsize(file_path)
    print(f"📊 File size: {file_size / (1024*1024):.2f} MB")
    
    try:
        # Generate a unique boundary for multipart form data
        import uuid
        boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
        
        # Create a generator that yields data in small chunks
        def generate_multipart_data():
            chunk_size = 8 * 1024 * 1024  # 8MB chunks - keeps memory usage low
            uploaded = 0
            
            # Form data header
            form_header = (
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            
            yield form_header
            
            # File content in small chunks
            with open(file_path, 'rb') as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    
                    uploaded += len(chunk)
                    
                    # Show progress every 16MB
                    if uploaded % (16 * 1024 * 1024) == 0 or uploaded == file_size:
                        progress = (uploaded / file_size) * 100
                        print(f"📦 Uploading: {uploaded / (1024*1024):.1f}MB / {file_size / (1024*1024):.1f}MB ({progress:.1f}%)")
                    
                    yield chunk
            
            # Form data footer
            form_footer = f"\r\n--{boundary}--\r\n".encode()
            yield form_footer
        
        # Prepare headers for multipart upload
        headers = {
            'User-Agent': 'Smart-TV-Streaming-Server/1.0',
            'Content-Type': f'multipart/form-data; boundary={boundary}'
        }
        
        print("📤 Starting memory-safe streaming upload...")
        
        # Use requests with generator data (this streams the data)
        response = requests.post(
            PIXELDRAIN_UPLOAD_URL,
            data=generate_multipart_data(),  # Generator - only keeps one chunk in memory
            headers=headers,
            auth=auth,
            verify=True,
            timeout=(120, 7200),  # 2 min connect, 2 hours read
            allow_redirects=True
        )
        
        print(f"📡 Response status: {response.status_code}")
        
        if response.status_code in [200, 201]:
            file_id = parse_pixeldrain_response_sync(response)
            print(f"✅ Upload successful! PixelDrain ID: {file_id}")
            return file_id
        else:
            error_text = response.text
            print(f"❌ Upload failed: {response.status_code} - {error_text}")
            
            # Handle specific errors
            if response.status_code == 401:
                raise Exception(f"Authentication failed - check API key: {error_text}")
            elif response.status_code == 413:
                raise Exception(f"File too large for PixelDrain: {error_text}")
            elif response.status_code == 429:
                raise Exception(f"Rate limited - try again later: {error_text}")
            else:
                raise Exception(f"Upload failed: {response.status_code} - {error_text}")
                
    except requests.exceptions.ConnectionError as e:
        print(f"❌ Connection error: {e}")
        raise Exception(f"Connection failed - check internet or try smaller file")
    except requests.exceptions.Timeout as e:
        print(f"❌ Timeout error: {e}")
        raise Exception(f"Upload timeout - file may be too large")
    except Exception as e:
        print(f"❌ Upload error: {e}")
        raise e

async def upload_with_requests_async(file_path: str, filename: str) -> str:
    """
    ⚠️ DEPRECATED - DO NOT USE ⚠️
    This function uses memory-hungry approach (loads entire file)
    Use upload_to_pixeldrain() instead for memory-safe uploads
    """
    print("⚠️ WARNING: Using deprecated upload function that loads entire file into memory!")
    print("🔧 Please use upload_to_pixeldrain() for memory-safe uploads")
    import asyncio
    import functools
    
    # Run the blocking upload in a thread pool
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, functools.partial(upload_with_requests_sync, file_path, filename))
    return result

def upload_with_requests_sync(file_path: str, filename: str) -> str:
    """
    ⚠️ DEPRECATED - DO NOT USE ⚠️ 
    This function is NOT memory-safe and loads entire file into memory
    Use memory_safe_upload_sync() through upload_to_pixeldrain() instead
    """
    print("⚠️ WARNING: Using deprecated upload function that loads entire file into memory!")
    print("🔧 Please use upload_to_pixeldrain() for memory-safe uploads")
    urllib3.disable_warnings()
    
    # Prepare headers according to PixelDrain API
    headers = {
        'User-Agent': 'Smart-TV-Streaming-Server/1.0'
    }
    
    # Add authentication if API key is available
    auth = None
    if PIXELDRAIN_API_KEY:
        auth = ('', PIXELDRAIN_API_KEY)
        print("🔑 Using API key authentication")
    else:
        print("⚠️ No API key - using anonymous upload (limited to 5GB/day)")
    
    file_size = os.path.getsize(file_path)
    print(f"📊 File size: {file_size / (1024*1024):.2f} MB")
    
    try:
        # Create a chunked file reader that only loads small chunks into memory
        class ChunkedFileUploader:
            def __init__(self, file_path, chunk_size=8 * 1024 * 1024):  # 8MB chunks
                self.file_path = file_path
                self.chunk_size = chunk_size
                self.total_size = os.path.getsize(file_path)
                self.uploaded = 0
                
            def __iter__(self):
                with open(self.file_path, 'rb') as f:
                    while True:
                        chunk = f.read(self.chunk_size)
                        if not chunk:
                            break
                        self.uploaded += len(chunk)
                        
                        # Show progress every 16MB or at end
                        if self.uploaded % (16 * 1024 * 1024) == 0 or self.uploaded == self.total_size:
                            progress = (self.uploaded / self.total_size) * 100
                            print(f"� Uploading: {self.uploaded / (1024*1024):.1f}MB / {self.total_size / (1024*1024):.1f}MB ({progress:.1f}%)")
                        
                        yield chunk
            
            def read(self, size=-1):
                # This method is required for requests to recognize it as a file-like object
                if not hasattr(self, '_iterator'):
                    self._iterator = iter(self)
                    self._buffer = b''
                
                if size == -1:
                    # Read all remaining data
                    result = self._buffer
                    try:
                        while True:
                            result += next(self._iterator)
                    except StopIteration:
                        pass
                    self._buffer = b''
                    return result
                else:
                    # Read specific amount
                    while len(self._buffer) < size:
                        try:
                            self._buffer += next(self._iterator)
                        except StopIteration:
                            break
                    
                    result = self._buffer[:size]
                    self._buffer = self._buffer[size:]
                    return result
        
        print("📤 Starting memory-safe chunked upload to PixelDrain...")
        
        # Create chunked uploader
        chunked_file = ChunkedFileUploader(file_path)
        
        # Use chunked file as file-like object
        files = {
            'file': (filename, chunked_file, 'application/octet-stream')
        }
        
        response = requests.post(
            PIXELDRAIN_UPLOAD_URL,
            files=files,
            headers=headers,
            auth=auth,
            verify=True,
            timeout=(120, 7200),  # 2 min connect, 2 hours read
            allow_redirects=True,
            stream=False
        )
        
        print(f"📡 Response status: {response.status_code}")
        
        if response.status_code == 201:
            file_id = parse_pixeldrain_response_sync(response)
            print(f"✅ Upload successful! PixelDrain ID: {file_id}")
            return file_id
        elif response.status_code == 200:
            file_id = parse_pixeldrain_response_sync(response)
            print(f"✅ Upload successful! PixelDrain ID: {file_id}")
            return file_id
        else:
            error_text = response.text
            print(f"❌ Upload failed: {response.status_code} - {error_text}")
            
            # Handle specific PixelDrain errors
            if response.status_code == 400:
                raise Exception(f"Bad request - file may be too large or invalid: {error_text}")
            elif response.status_code == 401:
                raise Exception(f"Authentication failed - check API key: {error_text}")
            elif response.status_code == 413:
                raise Exception(f"File too large for PixelDrain: {error_text}")
            elif response.status_code == 429:
                raise Exception(f"Rate limited by PixelDrain - try again later: {error_text}")
            else:
                raise Exception(f"PixelDrain upload failed: {response.status_code} - {error_text}")
            
    except requests.exceptions.ConnectionError as e:
        print(f"❌ Connection error: {e}")
        raise Exception(f"Connection failed - check internet connection or try smaller file")
    except requests.exceptions.Timeout as e:
        print(f"❌ Timeout error: {e}")
        raise Exception(f"Upload timeout - file may be too large for current connection")
    except Exception as e:
        print(f"❌ Upload error: {e}")
        raise e

def parse_pixeldrain_response_sync(response) -> str:
    """Parse PixelDrain response to extract file ID (sync version)"""
    try:
        result = response.json()
        file_id = result.get('id')
        if file_id:
            return file_id
        else:
            raise Exception("No file ID found in JSON response")
    except Exception as json_error:
        # Handle case where response is plain text (common with PixelDrain)
        result_text = response.text
        print(f"📄 Got plain text response: {result_text[:100]}...")
        
        # PixelDrain sometimes returns just the file ID as plain text
        if len(result_text.strip()) <= 20 and result_text.strip().isalnum():
            file_id = result_text.strip()
            print(f"✅ Extracted file ID from text response: {file_id}")
            return file_id
        else:
            # Try to extract ID from text response
            import re
            id_match = re.search(r'"id"\s*:\s*"([^"]+)"', result_text)
            if id_match:
                file_id = id_match.group(1)
                print(f"✅ Extracted file ID from text: {file_id}")
                return file_id
            else:
                print(f"❌ Could not parse file ID from response: {result_text}")
                raise Exception(f"Could not parse PixelDrain response: {json_error}")

async def check_pixeldrain_file_exists(file_id: str) -> bool:
    """Check if a file exists on PixelDrain"""
    try:
        url = f"https://pixeldrain.com/api/file/{file_id}"
        response = requests.head(url, timeout=10)
        return response.status_code == 200
    except:
        return False

async def delete_from_pixeldrain(file_id: str):
    """Delete a file from PixelDrain"""
    try:
        if not file_id:
            return
            
        headers = {}
        if PIXELDRAIN_API_KEY:
            import base64
            auth_string = base64.b64encode(f":{PIXELDRAIN_API_KEY}".encode()).decode()
            headers['Authorization'] = f'Basic {auth_string}'
        
        delete_url = f"https://pixeldrain.com/api/file/{file_id}"
        response = requests.delete(delete_url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            print(f"🗑️ Deleted from PixelDrain: {file_id}")
        else:
            print(f"⚠️ Delete failed (may already be expired): {file_id} - {response.status_code}")
            
    except Exception as e:
        print(f"❌ Error deleting from PixelDrain: {e}")

def get_pixeldrain_download_url(file_id: str) -> str:
    """Get direct download URL for PixelDrain file"""
    return f"https://pixeldrain.com/api/file/{file_id}"