import os
import requests
import pandas as pd
import datetime
from enum import Enum
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Security, status
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

# --- 初始化配置 ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
API_KEY_NAME = "X-API-KEY"
API_KEY = os.getenv("API_SECRET_KEY", "stan-default-secret")
BUCKET_NAME = "satellite-images"
TABLE_NAME = "satellite_images"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

app = FastAPI(title="Integrated Sensor & Weather API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 模型與常數 ---
class SensorData(BaseModel):
    temp: float
    humidity: float
    pm25: float
    co2: float

class MarqueeType(str, Enum):
    RAIN = "rain"
    WIND = "wind"
    PM25 = "pm25"

class MarqueeRequest(BaseModel):
    content: str
    category: MarqueeType

class MarqueeUpdate(BaseModel):
    is_active: bool

CATEGORY_CONFIG = {
    "rain": {"name": "降雨相關", "logo_url": "💦"},
    "wind": {"name": "風力相關", "logo_url": "💨"},
    "pm25": {"name": "PM2.5相關", "logo_url": "🌫️"}
}

DEVICES = ['ab170023', 'ab170019', 'ab170010']

# --- 工具函式 ---
async def get_api_key(api_key_from_header: str = Security(api_key_header)):
    if api_key_from_header == API_KEY:
        return api_key_from_header
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or Missing API Key")

class AeroboxManager:
    def __init__(self):
        self.supabase = supabase

    def fetch_data(self, device_id):
        now_taiwan = datetime.now(timezone(timedelta(hours=8)))
        after = (now_taiwan - timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%S') + 'Z'
        url = f'https://cv2-aerobox.synclytic.app/api/v1.1/aerobox/raw?device={device_id}&protocolVersion=v1&limit=1&after={after}'
        try:
            res = requests.get(url, timeout=10)
            res.raise_for_status()
            data = res.json()
            if not data: return None
            df = pd.json_normalize(data)
            return {
                "device_id": device_id,
                "device_time": df['DeviceTimestamp'].iloc[0],
                "pm25": float(df['Payload.Pm2_5'].iloc[0]),
                "co2": float(df['Payload.Co2'].iloc[0]),
                "temperature": float(df['Payload.Temp'].iloc[0]),
                "humidity": float(df['Payload.Rh'].iloc[0])
            }
        except Exception as e:
            print(f"{device_id} fetch failed: {e}")
            return None

    def save_to_db(self, data):
        try:
            return self.supabase.table("box").insert(data).execute()
        except Exception as e:
            print(f"Supabase Storage Error: {e}")
            return None

def fetch_weather_img(base_url, file_prefix, separator, time_format, sub_folder, img_type, ext, use_utc=True):
    headers = {'User-Agent': 'Mozilla/5.0'}
    for i in range(4):
        if use_utc:
            now = datetime.now(timezone.utc) - timedelta(minutes=20 + (i * 10))
        else:
            tz_taiwan = timezone(timedelta(hours=8))
            now = datetime.now(tz_taiwan) - timedelta(minutes=(i * 10))
            
        rounded_minute = (now.minute // 10) * 10
        timestamp = now.replace(minute=rounded_minute, second=0, microsecond=0)
        time_tag = timestamp.strftime(time_format)
        target_url = f"{base_url}{file_prefix}{separator}{time_tag}.{ext}"
        
        try:
            res = requests.get(target_url, headers=headers, timeout=10)
            if res.status_code == 200:
                time_tag_key = timestamp.strftime("%Y-%m-%d-%H-%M")
                file_path = f"cloud_image/{sub_folder}/{img_type}_{time_tag_key}.{ext}"
                supabase.storage.from_(BUCKET_NAME).upload(
                    path=file_path, file=res.content, 
                    file_options={"content-type": f"image/{ext}", "upsert": "true"}
                )
                db_data = {"obs_time": time_tag_key, "image_url": file_path, "type": img_type}
                db_res = supabase.table(TABLE_NAME).upsert(db_data, on_conflict="obs_time").execute()
                return db_res.data[0] if db_res.data else {}
        except:
            continue
    raise HTTPException(status_code=404, detail=f"Failed to fetch {img_type}")

# --- API 路由：感測器 ---
@app.get("/sensor/history")
async def get_sensor_history(device_id: str = "ab170023"):
    time_threshold = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    response = supabase.table("box").select("id, pm25, co2, temperature, humidity, created_at") \
        .eq("device_id", device_id).gte("created_at", time_threshold).order("created_at", desc=True).execute()
    return response.data

@app.get("/sensor/latest")
async def fetch_latest_sensors():
    manager = AeroboxManager()
    results = []
    for device_id in DEVICES:
        data = manager.fetch_data(device_id)
        if data:
            manager.save_to_db(data)
            results.append({"device_id": device_id, "status": "ok", "data": data})
        else:
            results.append({"device_id": device_id, "status": "not_found"})
    return {"results": results}

# --- API 路由：氣象雲圖 ---
@app.get("/stored-radar")
async def stored_radar():
    return fetch_weather_img("https://www.cwa.gov.tw/Data/radar/", "CV1_3600", "_", "%Y%m%d%H%M", "radar", "radar_echo", "png", False)

@app.get("/stored-satellite")
async def stored_satellite():
    return fetch_weather_img("https://www.cwa.gov.tw/Data/satellite/TWI_IR1_MB_800/", "TWI_IR1_MB_800", "-", "%Y-%m-%d-%H-%M", "cloud", "cloud_image", "jpg", True)

@app.get("/get-latest-radar")
async def get_latest_radar():
    latest_res = supabase.table(TABLE_NAME).select("obs_time").eq("type", "radar_echo").order("obs_time", desc=True).limit(1).execute()
    if not latest_res.data: return {"data_list": []}
    latest_dt = datetime.strptime(latest_res.data[0]["obs_time"], "%Y-%m-%d-%H-%M")
    three_hours_ago = (latest_dt - timedelta(hours=3)).strftime("%Y-%m-%d-%H-%M")
    response = supabase.table(TABLE_NAME).select("*").eq("type", "radar_echo").gte("obs_time", three_hours_ago).order("obs_time", desc=False).execute()
    return {"base_time": latest_res.data[0]["obs_time"], "data_list": response.data}

# --- API 路由：跑馬燈 ---
@app.post("/marquees", dependencies=[Depends(get_api_key)])
async def create_marquee(item: MarqueeRequest):
    config = CATEGORY_CONFIG.get(item.category.value)
    data = {"content": item.content, "category": item.category.value, "logo_url": config["logo_url"], "is_active": True}
    res = supabase.table("marquees").insert(data).execute()
    return {"status": "success", "data": res.data[0]}

@app.get("/marquees")
async def get_active_marquees():
    threshold = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    res = supabase.table("marquees").select("*").eq("is_active", True).gte("created_at", threshold).order("created_at", desc=True).execute()
    processed = []
    for row in (res.data or []):
        row["content"] = f"{row.get('logo_url', '')} {row.get('content', '')}".strip()
        processed.append(row)
    return {"status": "success", "data": processed}

@app.delete("/marquees/{marquee_id}", dependencies=[Depends(get_api_key)])
async def delete_marquee(marquee_id: int):
    supabase.table("marquees").delete().eq("id", marquee_id).execute()
    return {"status": "success", "message": f"Deleted ID: {marquee_id}"}