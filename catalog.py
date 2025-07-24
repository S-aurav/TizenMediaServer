import os
import json
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# Mount static directory for poster images
app.mount("/static", StaticFiles(directory="static"), name="static")

VIDEO_JSON_PATH = "video.json"

# Hardcoded poster mapping
POSTERS = {
    "Shinchan": "/static/shinchan.jpg",
    "Doraemon": "/static/dora.png",
    "Kiteretsu": "/static/kiteretsu.jpg",
    "Ninja Hattori": "/static/ninja.jpg",
    "Ninja Hattori Returns": "/static/ninjaReturns.jpg"
}

# Load catalog once on startup
try:
    with open(VIDEO_JSON_PATH, "r", encoding="utf-8") as f:
        catalog = json.load(f)
except Exception as e:
    print(f"Failed to load video.json: {e}")
    catalog = {}

@app.get("/catalog/series")
async def get_all_series():
    return [
        {"name": name, "poster": POSTERS.get(name, "")}
        for name in catalog.keys()
    ]

@app.get("/catalog/series/{series_name}")
async def get_seasons(series_name: str):
    series = catalog.get(series_name)
    if not series:
        raise HTTPException(status_code=404, detail="Series not found")
    return list(series.keys())

@app.get("/catalog/series/{series_name}/{season_name}")
async def get_episodes(series_name: str, season_name: str):
    series = catalog.get(series_name)
    if not series:
        raise HTTPException(status_code=404, detail="Series not found")
    season = series.get(season_name)
    if not season:
        raise HTTPException(status_code=404, detail="Season not found")
    return season
