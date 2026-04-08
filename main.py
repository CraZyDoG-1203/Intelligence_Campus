import uvicorn
from fastapi import FastAPI
from api import app as api_app  # 確保 api.py 在同一個目錄

app = FastAPI()

# 修正點：掛載 API
# 如果 api_app 內部的路由已經包含了完整路徑，直接掛載會讓路徑變長
# 建議：直接將 api.py 的路由整合，或確保前端呼叫路徑正確
app.mount("/api", api_app)

@app.get("/")
async def root():
    return {
        "message": "Integrated Sensor & Weather & Marquee API",
    }

if __name__ == "__main__":
    # 使用 main:app 啟動
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)