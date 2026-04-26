import os
import asyncio
import logging
from dataclasses import dataclass

import requests
import pandas as pd
from enum import Enum
from typing import Any, Dict, List
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Security, status
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware
from postgrest.exceptions import APIError

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 初始化配置 ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
API_KEY_NAME = "X-API-KEY"
API_KEY = os.getenv("API_SECRET_KEY")
BUCKET_NAME = "satellite-images"
TABLE_NAME = "satellite_images"
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    ).split(",")
    if origin.strip()
]
RADAR_IMAGE_TYPES = ["radar_echo"]
SATELLITE_IMAGE_TYPES = [
    "cloud_image",
    "visible_light",
    "visible_image",
    "satellite_visible",
    "cloud_visible",
    "visible",
]
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0"}
WEATHER_FETCH_INTERVAL_MINUTES = 10
WEATHER_UPSERT_CONFLICT = "obs_time,type"

# 檢查 Supabase 設定是否存在，避免啟動崩潰
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Missing SUPABASE_URL or SUPABASE_KEY")
if not API_KEY:
    raise ValueError("Missing API_SECRET_KEY")
if not ALLOWED_ORIGINS:
    raise ValueError("Missing ALLOWED_ORIGINS")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

app = FastAPI(title="Integrated Sensor & Weather API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
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

DEVICES = ["ab170023", "ab170019", "ab170010"]
PM25_CALIBRATION_SLOPE = 0.7
PM25_CALIBRATION_OFFSET = -10.18
INDOOR_TEMPERATURE_CALIBRATION_SLOPE = 1.06
INDOOR_TEMPERATURE_CALIBRATION_OFFSET = -2.28
OUTDOOR_TEMPERATURE_CALIBRATION_SLOPE = 1.16
OUTDOOR_TEMPERATURE_CALIBRATION_OFFSET = -4.63
TEMPERATURE_CALIBRATION_TYPE_BY_DEVICE = {
    "ab170010": "indoor",
    "ab170019": "indoor",
}
DEFAULT_TEMPERATURE_CALIBRATION_TYPE = "indoor"
TZ_TAIWAN = timezone(timedelta(hours=8))


@dataclass
class WeatherFetchResult:
    response: requests.Response | None = None
    error: Exception | None = None

# --- 工具函式 ---
async def get_api_key(api_key_from_header: str = Security(api_key_header)):
    if api_key_from_header == API_KEY:
        return api_key_from_header
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid API Key")


def coerce_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve_temperature_calibration_type(device_id: str | None) -> str:
    if not device_id:
        return DEFAULT_TEMPERATURE_CALIBRATION_TYPE
    return TEMPERATURE_CALIBRATION_TYPE_BY_DEVICE.get(
        device_id,
        DEFAULT_TEMPERATURE_CALIBRATION_TYPE,
    )


def calibrate_pm25(value: Any) -> float | None:
    raw_value = coerce_float(value)
    if raw_value is None:
        return None
    return round((PM25_CALIBRATION_SLOPE * raw_value) + PM25_CALIBRATION_OFFSET, 2)


def calibrate_temperature(value: Any, device_id: str | None) -> float | None:
    raw_value = coerce_float(value)
    if raw_value is None:
        return None

    calibration_type = resolve_temperature_calibration_type(device_id)
    if calibration_type == "outdoor":
        return round(
            (OUTDOOR_TEMPERATURE_CALIBRATION_SLOPE * raw_value) + OUTDOOR_TEMPERATURE_CALIBRATION_OFFSET,
            2,
        )

    return round(
        (INDOOR_TEMPERATURE_CALIBRATION_SLOPE * raw_value) + INDOOR_TEMPERATURE_CALIBRATION_OFFSET,
        2,
    )


def calibrate_sensor_payload(
    data: Dict[str, Any] | None,
    device_id: str | None = None,
) -> Dict[str, Any] | None:
    if not data:
        return data

    calibrated = dict(data)
    resolved_device_id = device_id or calibrated.get("device_id")
    calibrated["pm25"] = calibrate_pm25(calibrated.get("pm25"))
    calibrated["temperature"] = calibrate_temperature(
        calibrated.get("temperature"),
        resolved_device_id,
    )
    return calibrated

class AeroboxManager:
    def __init__(self):
        self.supabase = supabase

    async def fetch_data(self, device_id: str):
        after = format_utc_timestamp(datetime.now(timezone.utc) - timedelta(hours=24))
        url = f'https://cv2-aerobox.synclytic.app/api/v1.1/aerobox/raw?device={device_id}&protocolVersion=v1&limit=1&after={after}'
        try:
            res = await asyncio.to_thread(requests.get, url, timeout=10)
            res.raise_for_status()
            data = res.json()
            if not data:
                return None
            df = pd.json_normalize(data)
            return {
                "device_id": device_id,
                "device_time": df['DeviceTimestamp'].iloc[0],
                "pm25": float(df['Payload.Pm2_5'].iloc[0]),
                "co2": float(df['Payload.Co2'].iloc[0]),
                "temperature": float(df['Payload.Temp'].iloc[0]),
                "humidity": float(df['Payload.Rh'].iloc[0])
            }
        except requests.RequestException:
            logger.exception("Aerobox fetch failed for device=%s url=%s", device_id, url)
            return None

    async def save_to_db(self, data: Dict[str, Any]):
        try:
            return await asyncio.to_thread(self.supabase.table("box").insert(data).execute)
        except APIError:
            logger.exception("Supabase box insert failed payload=%s", data)
            return None


async def get_latest_sensor_from_db(device_id: str):
    response = await asyncio.to_thread(
        supabase.table("box")
        .select("id, device_id, device_time, pm25, co2, temperature, humidity")
        .eq("device_id", device_id)
        .order("device_time", desc=True)
        .limit(1)
        .execute
    )

    rows = response.data or []
    return calibrate_sensor_payload(rows[0]) if rows else None

def format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_time_tag_key(timestamp: datetime) -> str:
    return timestamp.strftime("%Y-%m-%d-%H-%M")


async def upload_to_storage(file_path: str, content: bytes, ext: str):
    return await asyncio.to_thread(
        supabase.storage.from_(BUCKET_NAME).upload,
        path=file_path,
        file=content,
        file_options={"content-type": f"image/{ext}", "upsert": "true"},
    )


async def delete_from_storage(file_path: str):
    return await asyncio.to_thread(supabase.storage.from_(BUCKET_NAME).remove, [file_path])


async def save_weather_record(obs_time: str, image_url: str, img_type: str):
    db_data = {"obs_time": obs_time, "image_url": image_url, "type": img_type}
    logger.info(
        "Upserting weather record type=%s obs_time=%s image_url=%s",
        img_type,
        obs_time,
        image_url,
    )
    return await asyncio.to_thread(
        supabase.table(TABLE_NAME).upsert(
            db_data,
            on_conflict=WEATHER_UPSERT_CONFLICT,
        ).execute
    )


async def persist_weather_image(obs_time: str, img_type: str, file_path: str, content: bytes, ext: str):
    try:
        await upload_to_storage(file_path, content, ext)
        logger.info(
            "Uploaded weather image type=%s obs_time=%s storage_path=%s",
            img_type,
            obs_time,
            file_path,
        )
    except APIError:
        logger.exception(
            "Storage upload failed type=%s obs_time=%s storage_path=%s",
            img_type,
            obs_time,
            file_path,
        )
        raise HTTPException(status_code=502, detail=f"Failed to upload {img_type}")

    try:
        await save_weather_record(obs_time, file_path, img_type)
    except APIError:
        logger.exception(
            "Weather record upsert failed type=%s obs_time=%s storage_path=%s; deleting uploaded file",
            img_type,
            obs_time,
            file_path,
        )
        try:
            await delete_from_storage(file_path)
            logger.info(
                "Deleted uploaded file after DB failure type=%s obs_time=%s storage_path=%s",
                img_type,
                obs_time,
                file_path,
            )
        except APIError:
            logger.exception(
                "Failed to delete uploaded file after DB failure type=%s obs_time=%s storage_path=%s",
                img_type,
                obs_time,
                file_path,
            )
        raise HTTPException(status_code=502, detail=f"Failed to save {img_type} metadata")

    return {"obs_time": obs_time, "image_url": file_path, "type": img_type}


async def fetch_weather_img(
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
    current_tz = timezone.utc if use_utc else TZ_TAIWAN
    last_result = WeatherFetchResult()

    for i in range(max_attempts):
        now = datetime.now(current_tz) - timedelta(minutes=initial_delay_minutes + (i * 10))
        rounded_minute = (now.minute // WEATHER_FETCH_INTERVAL_MINUTES) * WEATHER_FETCH_INTERVAL_MINUTES
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
            res = await asyncio.to_thread(
                requests.get,
                target_url,
                headers=REQUEST_HEADERS,
                timeout=10,
            )
            last_result = WeatherFetchResult(response=res)
            logger.info(
                "Weather image response type=%s attempt=%s status=%s",
                img_type,
                i + 1,
                res.status_code,
            )
            if res.status_code == 200:
                time_tag_key = build_time_tag_key(timestamp)
                file_path = f"cloud_image/{sub_folder}/{img_type}_{time_tag_key}.{ext}"
                return await persist_weather_image(
                    obs_time=time_tag_key,
                    img_type=img_type,
                    file_path=file_path,
                    content=res.content,
                    ext=ext,
                )
        except requests.RequestException as exc:
            last_result = WeatherFetchResult(error=exc)
            logger.exception(
                "Weather image fetch failed type=%s attempt=%s url=%s error=%s",
                img_type,
                i + 1,
                target_url,
                exc,
            )
            continue

    logger.warning("Weather image fetch exhausted type=%s attempts=%s", img_type, max_attempts)
    if last_result.error is not None:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to reach upstream weather source for {img_type}",
        )

    last_status_code = last_result.response.status_code if last_result.response is not None else "unknown"
    raise HTTPException(
        status_code=404,
        detail=f"No recent {img_type} image available from upstream source (last status: {last_status_code})",
    )


def build_public_url(storage_path: str) -> str:
    if not storage_path:
        return ""

    public_url_response = supabase.storage.from_(BUCKET_NAME).get_public_url(storage_path)
    if isinstance(public_url_response, dict):
        return public_url_response.get("publicURL", "")
    return public_url_response


async def fetch_recent_weather_images(image_types: List[str], hours: int = 3):
    time_threshold = build_time_tag_key(datetime.now(TZ_TAIWAN) - timedelta(hours=hours))
    response = await asyncio.to_thread(
        supabase.table(TABLE_NAME)
        .select("id, obs_time, image_url, type")
        .in_("type", image_types)
        .gte("obs_time", time_threshold)
        .order("obs_time", desc=True)
        .execute
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
async def get_sensor_history(device_id: str):
    time_threshold = format_utc_timestamp(datetime.now(timezone.utc) - timedelta(hours=24))
    response = await asyncio.to_thread(
        supabase.table("box")
        .select("id, device_time, pm25, co2, temperature, humidity")
        .eq("device_id", device_id)
        .gte("device_time", time_threshold)
        .order("device_time", desc=True)
        .execute
    )
    return [
        calibrate_sensor_payload(row, device_id=device_id)
        for row in (response.data or [])
    ]

@app.get("/sensor/latest")
async def fetch_latest_sensors(device_id: str | None = None):
    if device_id:
        data = await get_latest_sensor_from_db(device_id)
        if data:
            return {"device_id": device_id, "status": "ok", "data": data}
        return {"device_id": device_id, "status": "not_found"}

    results = []
    for current_device_id in DEVICES:
        data = await get_latest_sensor_from_db(current_device_id)
        if data:
            results.append({"device_id": current_device_id, "status": "ok", "data": data})
        else:
            results.append({"device_id": current_device_id, "status": "not_found"})
    return {"results": results}


@app.get("/sensor/fetch-latest", dependencies=[Depends(get_api_key)])
async def sync_latest_sensors():
    manager = AeroboxManager()
    results = []
    for device_id in DEVICES:
        data = await manager.fetch_data(device_id)
        if data:
            await manager.save_to_db(data)
            results.append(
                {
                    "device_id": device_id,
                    "status": "ok",
                    "data": calibrate_sensor_payload(data, device_id=device_id),
                }
            )
        else:
            results.append({"device_id": device_id, "status": "not_found"})
    return {"results": results}

@app.get("/stored-radar", dependencies=[Depends(get_api_key)])
async def stored_radar():
    return await fetch_weather_img("https://www.cwa.gov.tw/Data/radar/", "CV1_3600", "_", "%Y%m%d%H%M", "radar", "radar_echo", "png", False)

@app.get("/stored-satellite", dependencies=[Depends(get_api_key)])
async def stored_satellite():
    return await fetch_weather_img(
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
    radar_data, satellite_data = await asyncio.gather(
        fetch_recent_weather_images(RADAR_IMAGE_TYPES, hours=3),
        fetch_recent_weather_images(SATELLITE_IMAGE_TYPES, hours=3),
    )
    return {
        "status": "success",
        "hours": 3,
        "radar": radar_data["data"],
        "satellite": satellite_data["data"],
    }

@app.get("/marquees")
async def get_active_marquees():
    threshold = format_utc_timestamp(datetime.now(timezone.utc) - timedelta(hours=24))
    res = await asyncio.to_thread(
        supabase.table("marquees")
        .select("*")
        .eq("is_active", True)
        .gte("created_at", threshold)
        .order("created_at", desc=True)
        .execute
    )
    processed = []
    for row in (res.data or []):
        row["content"] = f"{row.get('logo_url', '')} {row.get('content', '')}".strip()
        processed.append(row)
    return {"status": "success", "data": processed}

@app.post("/marquees", dependencies=[Depends(get_api_key)])
async def create_marquee(item: MarqueeRequest):
    config = CATEGORY_CONFIG.get(item.category.value)
    data = {"content": item.content, "category": item.category.value, "logo_url": config["logo_url"], "is_active": True}
    res = await asyncio.to_thread(supabase.table("marquees").insert(data).execute)
    return {"status": "success", "data": res.data[0]}

@app.delete("/marquees/{marquee_id}", dependencies=[Depends(get_api_key)])
async def delete_marquee(marquee_id: int):
    await asyncio.to_thread(supabase.table("marquees").delete().eq("id", marquee_id).execute)
    return {"status": "success", "message": f"Deleted ID: {marquee_id}"}