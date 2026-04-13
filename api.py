import os
import logging
import requests
import pandas as pd
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 初始化配置 ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
API_KEY_NAME = "X-API-KEY"
API_KEY = os.getenv("API_SECRET_KEY", "stan-default-secret")
BUCKET_NAME = "satellite-images"
TABLE_NAME = "satellite_images"
RADAR_IMAGE_TYPES = ["radar_echo"]
SATELLITE_IMAGE_TYPES = [
    "cloud_image",
    "visible_light",
    "visible_image",
    "satellite_visible",
    "cloud_visible",
    "visible",
]

# 檢查 Supabase 設定是否存在，避免啟動崩潰
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Missing SUPABASE_URL or SUPABASE_KEY")

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
class MarqueeType(str, Enum):
    RAIN = "rain"
    WIND = "wind"
    PM25 = "pm25"

class MarqueeRequest(BaseModel):
    content: str
    category: MarqueeType

CATEGORY_CONFIG = {
    "rain": {"name": "降雨相關", "logo_url": "💦"},
    "wind": {"name": "風力相關", "logo_url": "💨"},
    "pm25": {"name": "PM2.5相關", "logo_url": "🌫️"}
}

DEVICES = ['ab170023', 'ab170019', 'ab170010']
TZ_TAIWAN = timezone(timedelta(hours=8))

# --- 工具函式 ---
async def get_api_key(api_key_from_header: str = Security(api_key_header)):
    if api_key_from_header == API_KEY:
        return api_key_from_header
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid API Key")

class AeroboxManager:
    def __init__(self):
        self.supabase = supabase

    def fetch_data(self, device_id):
        # 修正時區設定
        tz_tw = timezone(timedelta(hours=8))
        now_taiwan = datetime.now(tz_tw)
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
            print(f"Supabase Error: {e}")
            return None

def fetch_weather_img(
    base_url,
    file_prefix,
    separator,
    time_format,
    sub_folder,
    img_type,
    ext,
    use_utc=True,
    initial_delay_minutes=20,
    max_attempts=12,
):
    headers = {'User-Agent': 'Mozilla/5.0'}
    current_tz = timezone.utc if use_utc else TZ_TAIWAN

    for i in range(max_attempts):
        now = datetime.now(current_tz) - timedelta(minutes=initial_delay_minutes + (i * 10))
        rounded_minute = (now.minute // 10) * 10
        timestamp = now.replace(minute=rounded_minute, second=0, microsecond=0)
        time_tag = timestamp.strftime(time_format)
        target_url = f"{base_url}{file_prefix}{separator}{time_tag}.{ext}"

        logger.info(
            "Fetching weather image type=%s attempt=%s timestamp=%s url=%s",
            img_type,
            i + 1,
            timestamp.isoformat(),
            target_url,
        )

        try:
            res = requests.get(target_url, headers=headers, timeout=10)
            logger.info(
                "Weather image response type=%s attempt=%s status=%s",
                img_type,
                i + 1,
                res.status_code,
            )
            if res.status_code == 200:
                time_tag_key = timestamp.strftime("%Y-%m-%d-%H-%M")
                file_path = f"cloud_image/{sub_folder}/{img_type}_{time_tag_key}.{ext}"
                supabase.storage.from_(BUCKET_NAME).upload(
                    path=file_path, file=res.content, 
                    file_options={"content-type": f"image/{ext}", "upsert": "true"}
                )
                logger.info(
                    "Uploaded weather image type=%s obs_time=%s storage_path=%s",
                    img_type,
                    time_tag_key,
                    file_path,
                )
                return save_weather_record(time_tag_key, file_path, img_type)
        except Exception as exc:
            logger.exception(
                "Weather image fetch failed type=%s attempt=%s url=%s error=%s",
                img_type,
                i + 1,
                target_url,
                exc,
            )
            continue

    logger.warning("Weather image fetch exhausted type=%s attempts=%s", img_type, max_attempts)
    raise HTTPException(status_code=404, detail=f"Failed to fetch {img_type}")


def build_public_url(storage_path: str) -> str:
    if not storage_path:
        return ""

    public_url_response = supabase.storage.from_(BUCKET_NAME).get_public_url(storage_path)
    if isinstance(public_url_response, dict):
        return public_url_response.get("publicURL", "")
    return public_url_response


def save_weather_record(obs_time: str, image_url: str, img_type: str):
    db_data = {"obs_time": obs_time, "image_url": image_url, "type": img_type}
    existing = (
        supabase.table(TABLE_NAME)
        .select("id")
        .eq("obs_time", obs_time)
        .eq("type", img_type)
        .limit(1)
        .execute()
    )

    if existing.data:
        record_id = existing.data[0]["id"]
        logger.info(
            "Updating weather record id=%s type=%s obs_time=%s image_url=%s",
            record_id,
            img_type,
            obs_time,
            image_url,
        )
        supabase.table(TABLE_NAME).update(db_data).eq("id", record_id).execute()
    else:
        logger.info(
            "Inserting weather record type=%s obs_time=%s image_url=%s",
            img_type,
            obs_time,
            image_url,
        )
        supabase.table(TABLE_NAME).insert(db_data).execute()

    return db_data


def fetch_recent_weather_images(image_types: List[str], hours: int = 3):
    time_threshold = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d-%H-%M")
    response = (
        supabase.table(TABLE_NAME)
        .select("id, obs_time, image_url, type")
        .in_("type", image_types)
        .gte("obs_time", time_threshold)
        .order("obs_time", desc=True)
        .execute()
    )

    items = []
    for row in response.data or []:
        storage_path = row.get("image_url", "")
        items.append(
            {
                **row,
                "public_url": build_public_url(storage_path),
            }
        )

    return {
        "status": "success",
        "hours": hours,
        "count": len(items),
        "data": items,
    }

# --- API 路由 ---
@app.get("/sensor/history")
async def get_sensor_history(device_id: str = "ab170023"):
    time_threshold = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    # 將 desc 設為 True，這樣最新的資料就會排在 Array 的第一個 (Index 0)
    response = supabase.table("box") \
        .select("id, pm25, co2, temperature, humidity, created_at") \
        .eq("device_id", device_id) \
        .gte("created_at", time_threshold) \
        .order("created_at", desc=True) \
        .execute()
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

@app.get("/stored-radar")
async def stored_radar():
    return fetch_weather_img("https://www.cwa.gov.tw/Data/radar/", "CV1_3600", "_", "%Y%m%d%H%M", "radar", "radar_echo", "png", False)

@app.get("/stored-satellite")
async def stored_satellite():
    return fetch_weather_img(
        "https://www.cwa.gov.tw/Data/satellite/TWI_IR1_MB_800/",
        "TWI_IR1_MB_800",
        "-",
        "%Y-%m-%d-%H-%M",
        "cloud",
        "cloud_image",
        "jpg",
        use_utc=False,
        initial_delay_minutes=20,
        max_attempts=12,
    )


@app.get("/last-3-hours")
async def get_last_3_hours_weather():
    return {
        "status": "success",
        "hours": 3,
        "radar": fetch_recent_weather_images(RADAR_IMAGE_TYPES, hours=3)["data"],
        "satellite": fetch_recent_weather_images(SATELLITE_IMAGE_TYPES, hours=3)["data"],
    }

@app.get("/marquees")
async def get_active_marquees():
    threshold = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    res = supabase.table("marquees").select("*").eq("is_active", True).gte("created_at", threshold).order("created_at", desc=True).execute()
    processed = []
    for row in (res.data or []):
        row["content"] = f"{row.get('logo_url', '')} {row.get('content', '')}".strip()
        processed.append(row)
    return {"status": "success", "data": processed}

@app.post("/marquees", dependencies=[Depends(get_api_key)])
async def create_marquee(item: MarqueeRequest):
    config = CATEGORY_CONFIG.get(item.category.value)
    data = {"content": item.content, "category": item.category.value, "logo_url": config["logo_url"], "is_active": True}
    res = supabase.table("marquees").insert(data).execute()
    return {"status": "success", "data": res.data[0]}

@app.delete("/marquees/{marquee_id}", dependencies=[Depends(get_api_key)])
async def delete_marquee(marquee_id: int):
    supabase.table("marquees").delete().eq("id", marquee_id).execute()
    return {"status": "success", "message": f"Deleted ID: {marquee_id}"}
