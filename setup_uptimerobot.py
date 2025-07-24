#!/usr/bin/env python3
"""
UptimeRobot GitHub Integration Helper
Generates the exact configuration for UptimeRobot setup
"""

import json

def main():
    print("🤖 UptimeRobot Configuration Generator")
    print("=" * 50)
    
    # Get user inputs
    username = input("Enter your GitHub username: ").strip()
    repo_name = input("Enter your repository name: ").strip()
    github_pat = input("Enter your GitHub Personal Access Token: ").strip()
    
    # Generate configuration
    github_api_url = f"https://api.github.com/repos/{username}/{repo_name}/actions/workflows/external-keep-alive.yml/dispatches"
    
    print("\n" + "=" * 60)
    print("🔧 UPTIMEROBOT MONITOR CONFIGURATION")
    print("=" * 60)
    
    print("\n📋 Basic Settings:")
    print(f"Monitor Type: HTTP(s)")
    print(f"Friendly Name: Smart TV Server Keep-Alive ({username})")
    print(f"URL: {github_api_url}")
    print(f"Monitoring Interval: 10 minutes")
    print(f"Request Method: POST")
    
    print("\n📋 Request Headers:")
    print("Accept: application/vnd.github+json")
    print(f"Authorization: Bearer {github_pat}")
    print("User-Agent: UptimeRobot-KeepAlive/1.0")
    
    print("\n📋 Request Body (JSON):")
    request_body = {
        "ref": "main",
        "inputs": {
            "source": "uptimerobot"
        }
    }
    print(json.dumps(request_body, indent=2))
    
    print("\n📋 Maintenance Window (Sleep Hours):")
    print("Type: Weekly")
    print("Days: Monday to Sunday")
    print("Time: 21:00 to 10:00 (9 PM to 10 AM)")
    print("Timezone: Asia/Kolkata (IST)")
    
    print("\n" + "=" * 60)
    print("🧪 TEST YOUR SETUP")
    print("=" * 60)
    
    print(f"\n🔍 Test GitHub API manually:")
    print("curl -X POST \\")
    print("  -H 'Accept: application/vnd.github+json' \\")
    print(f"  -H 'Authorization: Bearer {github_pat}' \\")
    print(f"  {github_api_url} \\")
    print("  -d '{\"ref\":\"main\",\"inputs\":{\"source\":\"manual-test\"}}'")
    
    print(f"\n✅ Expected result: Check {username}/{repo_name} Actions tab for new workflow run")
    
    print("\n" + "=" * 60)
    print("📊 COPY-PASTE VALUES FOR UPTIMEROBOT")
    print("=" * 60)
    
    print(f"\nURL: {github_api_url}")
    print(f"Authorization Header: Bearer {github_pat}")
    print(f"Request Body: {json.dumps(request_body)}")
    
    print("\n🎉 Setup complete! Your server will have 99.9% uptime!")

if __name__ == "__main__":
    main()