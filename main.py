import uvicorn
from fastapi import FastAPI
from api import app as api_app

app = FastAPI()

# 將所有在 api.py 定義的路由掛載到 /api 下
app.mount("/api", api_app)

@app.get("/")
async def root():
    return {
        "message": "Integrated Sensor & Weather & Marquee API is active",
        "endpoints": {
            "sensors": "/api/sensor/latest",
            "weather": "/api/get-latest-radar",
            "marquees": "/api/marquees",
            "docs": "/docs"
        }
    }

if __name__ == "__main__":
    # 啟動時請確保檔案名稱為 main.py
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)