import uvicorn
from fastapi import FastAPI
from api import app as api_app, stored_radar, stored_satellite
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/api", api_app)


@app.get("/stored-radar")
async def stored_radar_alias():
    return await stored_radar()


@app.get("/stored-satellite")
async def stored_satellite_alias():
    return await stored_satellite()

@app.get("/")
async def root():
    return {
        "message": "Integrated Sensor & Weather & Marquee API",
    }

if __name__ == "__main__":
    # 使用 main:app 啟動
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)