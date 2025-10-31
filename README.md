# TizenMediaServer 🎬

[![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![Deployment](https://img.shields.io/badge/Deployment-Render-purple.svg)](https://render.com/)

A comprehensive Netflix-like streaming platform that enables automated content management, streaming, and distribution across multiple platforms including Smart TVs (Samsung Tizen), mobile devices, and web browsers.

## 🌟 Features

### 🎯 Core Functionality
- **Multi-Platform Streaming**: Supports Smart TVs (Tizen OS), web browsers, and mobile devices
- **Automated Content Pipeline**: Downloads and processes content from Telegram channels
- **Cloud Storage Integration**: Seamlessly uploads to PixelDrain with intelligent management

### 🔧 Advanced Features
- **Queue Management**: Intelligent download scheduling and progress tracking
- **Memory-Safe Operations**: Handles large video files (multi-GB) efficiently
- **Proxy Server**: Content delivery optimization
- **GitHub Gist Integration**: Metadata storage and synchronization
- **Background Tasks**: Automated processing with error handling

### 📱 Supported Platforms
- **Samsung Tizen Smart TVs** (Tizen 4.0+)
- **Web Browsers** (Chrome, Firefox, Safari, Edge)

## 🏗️ Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Frontend      │    │   Backend API    │    │  External APIs  │
│                 │    │                  │    │                 │
│ • Web Interface │◄──►│ • FastAPI Server │◄──►│ • Telegram API  │
│ • Mobile App    │    │ • REST Endpoints │    │ • PixelDrain    │
│ • Tizen TV App  │    │ • WebSocket      │    │ • GitHub Gist   │
└─────────────────┘    └──────────────────┘    └─────────────────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │   Data Layer     │
                    │                  │
                    │ • File Storage   │
                    │ • Cache Layer    │
                    └──────────────────┘
```

## 🚀 Quick Start

### Prerequisites
- Python 3.9+
- Node.js 16+
- Telegram API credentials
- PixelDrain account

### 1. Clone Repository
```bash
git clone https://github.com/S-aurav/TizenMediaServer.git
cd TizenMediaServer
```

### 2. Backend Setup
```bash
# Install Python dependencies
pip install -r requirements.txt

# Environment Configuration
Create a .env file with the following variables:

# Telegram Configuration
API_ID=your_telegram_api_id
API_HASH=your_telegram_api_hash
PHONE_NUMBER=your_phone_number
TELEGRAM_CHANNEL=your_channel_name
TELEGRAM_SESSION_STRING=your_session_string

# Storage Configuration
PIXELDRAIN_API_KEY=your_pixeldrain_api_key
PIXELDRAIN_USERNAME=your_username

# GitHub Integration
GITHUB_TOKEN=your_github_token
GIST_ID=your_gist_id
VIDEO_GIST_ID=your_video_gist_id

# Monitoring (Optional)
NEW_RELIC_KEY=your_newrelic_key
```

### 4. Start Backend Server
```bash
# Development
python main_server.py

### Base URL
- Development: `http://localhost:8000`
- Production: `https://tizenmediaserver.onrender.com`

```

## 🖥️ Frontend Applications

### Mobile Interface
- **Path**: `/mobile/`
- **Features**: Touch-optimized, offline support, push notifications
- **Technologies**: Progressive Web App (PWA)

### Tizen TV Application
- **Path**: `/tizen player/`
- **Features**: Remote control support, 4K streaming, Samsung TV integration
- **Technologies**: Tizen Web API, HTML5 Video



## 🔧 Configuration

### Server Configuration
- **Memory Management**: Optimized for large file handling
- **Concurrent Downloads**: Configurable worker count
- **Streaming Quality**: Adaptive bitrate support
- **Cache Management**: Intelligent cleanup policies

### Storage Configuration
```python
# Upload thresholds
CHUNK_SIZE = 100 * 1024 * 1024  # 100MB chunks
MAX_CONCURRENT_UPLOADS = 3
RETRY_ATTEMPTS = 3
CLEANUP_INTERVAL = 3600  # 1 hour
```

### Frontend Configuration
```javascript
// API endpoints
const API_BASE = 'https://tizenmediaserver.onrender.com';
const STREAMING_ENDPOINT = '/stream_mobile/';
const CATALOG_ENDPOINT = '/catalog/series';
```


## 📊 Monitoring & Analytics

### New Relic Integration
```python
# Automatic performance monitoring
- Application performance metrics
- Error tracking and alerting
- Database query optimization
- Custom event tracking
```

### Health Checks
```http
GET /health              # Basic health check
```

### Documentation
- [API Documentation](http://172.188.40.236:8000/docs)

