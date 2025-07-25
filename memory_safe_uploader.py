# filepath: c:\Users\yadav\OneDrive\Desktop\python Bot\memory_safe_uploader.py
import os
import time
import asyncio
import requests
import urllib3
import uuid
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
PIXELDRAIN_API_KEY = os.getenv("PIXELDRAIN_API_KEY")
PIXELDRAIN_UPLOAD_URL = "https://pixeldrain.com/api/file"

# Debug: Print API key status
if PIXELDRAIN_API_KEY:
    print(f"🔑 PixelDrain API Key loaded: {PIXELDRAIN_API_KEY[:8]}...{PIXELDRAIN_API_KEY[-4:]}")
else:
    print("⚠️ No PixelDrain API Key found")

async def memory_safe_upload_to_pixeldrain(file_path: str, filename: str) -> str:
    """Memory-safe upload that only uses ~8-16MB of RAM regardless of file size"""
    max_retries = 3
    retry_delay = 5
    
    for attempt in range(max_retries):
        try:
            print(f"🚀 Upload attempt {attempt + 1}/{max_retries} to PixelDrain: {filename}")
            
            # Use the memory-safe upload method
            return await streaming_upload_sync(file_path, filename)
                
        except Exception as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
                retry_delay *= 2
                continue
            else:
                raise Exception(f"Upload failed after {max_retries} attempts: {e}")

async def streaming_upload_sync(file_path: str, filename: str) -> str:
    """Run the blocking upload in a thread pool"""
    import asyncio
    import functools
    
    # Run the blocking upload in a thread pool
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, functools.partial(memory_safe_upload_sync, file_path, filename))
    return result

def memory_safe_upload_sync(file_path: str, filename: str) -> str:
    """TRUE memory-safe upload using generator-based streaming"""
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
            file_id = parse_pixeldrain_response(response)
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

def parse_pixeldrain_response(response) -> str:
    """Parse PixelDrain response to extract file ID"""
    try:
        result = response.json()
        file_id = result.get('id')
        if file_id:
            return file_id
        else:
            raise Exception("No file ID found in JSON response")
    except Exception as json_error:
        # Handle plain text response
        result_text = response.text
        print(f"📄 Got plain text response: {result_text[:100]}...")
        
        # PixelDrain sometimes returns just the file ID
        if len(result_text.strip()) <= 20 and result_text.strip().isalnum():
            file_id = result_text.strip()
            print(f"✅ Extracted file ID: {file_id}")
            return file_id
        else:
            # Try regex extraction
            import re
            id_match = re.search(r'"id"\s*:\s*"([^"]+)"', result_text)
            if id_match:
                file_id = id_match.group(1)
                print(f"✅ Extracted file ID: {file_id}")
                return file_id
            else:
                print(f"❌ Could not parse response: {result_text}")
                raise Exception(f"Could not parse PixelDrain response: {json_error}")