#!/usr/bin/env python3
"""
Server Performance Optimizer
Run this after deployment to optimize chunk sizes for maximum download speed.
"""

import requests
import json
import time

SERVER_URL = "https://your-app.onrender.com"  # Replace with your Render URL

def test_server_performance():
    """Test server performance and get recommendations"""
    print("🧪 Testing server performance...")
    
    try:
        response = requests.get(f"{SERVER_URL}/performance/test", timeout=30)
        if response.status_code == 200:
            data = response.json()
            print("\n📊 Server Performance Results:")
            print("=" * 50)
            
            metrics = data.get("performance_metrics", {})
            print(f"💾 Memory Speed: {metrics.get('memory_allocation_mbps', 0):.2f} MB/s")
            print(f"💿 Disk Write Speed: {metrics.get('disk_write_mbps', 0):.2f} MB/s")
            print(f"📖 Disk Read Speed: {metrics.get('disk_read_mbps', 0):.2f} MB/s")
            
            recommendations = data.get("recommendations", {})
            print(f"\n🎯 Performance Tier: {recommendations.get('performance_tier', 'Unknown')}")
            print(f"📈 Recommended Max Chunk: {recommendations.get('suggested_max_chunk_mb', 0)} MB")
            print(f"📈 Recommended Default Chunk: {recommendations.get('suggested_default_chunk_mb', 0)} MB")
            
            return recommendations
            
        else:
            print(f"❌ Server test failed: {response.status_code}")
            return None
            
    except Exception as e:
        print(f"❌ Error testing server: {e}")
        return None

def optimize_chunk_sizes(recommendations):
    """Apply optimal chunk sizes based on server performance"""
    if not recommendations:
        print("⚠️ No recommendations available, using defaults")
        return False
    
    max_chunk = recommendations.get('suggested_max_chunk_mb', 16)
    default_chunk = recommendations.get('suggested_default_chunk_mb', 2)
    
    print(f"\n🔧 Applying optimizations...")
    print(f"Setting max chunk size to: {max_chunk} MB")
    print(f"Setting default chunk size to: {default_chunk} MB")
    
    try:
        response = requests.post(
            f"{SERVER_URL}/performance/configure",
            params={
                "max_chunk_mb": max_chunk,
                "default_chunk_mb": default_chunk,
                "adaptive": True
            },
            timeout=30
        )
        
        if response.status_code == 200:
            data = response.json()
            print("✅ Configuration updated successfully!")
            
            changes = data.get("changes", {})
            if changes:
                print("\n📝 Changes applied:")
                for key, change in changes.items():
                    print(f"  {key}: {change['old']} → {change['new']}")
            
            return True
        else:
            print(f"❌ Configuration failed: {response.status_code}")
            return False
            
    except Exception as e:
        print(f"❌ Error configuring server: {e}")
        return False

def test_health():
    """Quick health check"""
    try:
        response = requests.get(f"{SERVER_URL}/health", timeout=10)
        if response.status_code == 200:
            data = response.json()
            print(f"✅ Server healthy - Telegram: {data.get('telegram_status', 'unknown')}")
            return True
        else:
            print(f"❌ Health check failed: {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ Health check error: {e}")
        return False

def main():
    print("🚀 Smart TV Streaming Server Optimizer")
    print("=" * 50)
    
    # Update server URL
    global SERVER_URL
    user_url = input(f"Enter your server URL (or press Enter for {SERVER_URL}): ").strip()
    if user_url:
        SERVER_URL = user_url.rstrip('/')
    
    print(f"🎯 Target server: {SERVER_URL}")
    
    # Test server health
    if not test_health():
        print("❌ Server is not responding. Check deployment status.")
        return
    
    # Test performance
    recommendations = test_server_performance()
    
    if recommendations:
        # Ask user if they want to apply optimizations
        apply = input("\n🔧 Apply recommended optimizations? (y/N): ").strip().lower()
        if apply in ['y', 'yes']:
            if optimize_chunk_sizes(recommendations):
                print("\n🎉 Server optimized for maximum download speed!")
                print("📈 New downloads will automatically use optimal chunk sizes")
                print("🔄 Adaptive chunking will further optimize during downloads")
            else:
                print("\n⚠️ Optimization failed, server will use default settings")
        else:
            print("\n⏭️ Skipped optimization, using current settings")
    
    print("\n✅ Optimization complete!")
    print("🎬 Your streaming server is ready for maximum performance!")

if __name__ == "__main__":
    main()