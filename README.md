# Intelligence Campus API

一個使用 FastAPI 建立的整合型後端服務，提供校園感測器資料、天氣影像快取，以及跑馬燈公告管理功能。

目前專案整合了以下能力：

- 感測器最新資料擷取與歷史查詢
- 中央氣象署雷達與衛星影像抓取後儲存到 Supabase Storage
- 跑馬燈公告查詢、建立、刪除
- 可部署到 Vercel

## Tech Stack

- FastAPI
- Uvicorn
- Supabase
- Requests
- Pandas
- python-dotenv

## Project Structure

```text
.
├── api.py          # API 路由與主要商業邏輯
├── main.py         # FastAPI 入口，掛載 /api
├── requirements.txt
├── vercel.json     # Vercel 部署設定
└── README.md
```

## API Overview

根路由：

- `GET /`
  - 回傳服務簡介訊息

API 掛載在 `/api` 之下，因此實際呼叫路徑如下：

### Sensor

- `GET /api/sensor/history?device_id=ab170023`
  - 取得指定裝置近 24 小時的歷史資料
- `GET /api/sensor/latest`
  - 讀取預設裝置清單的最新感測器資料，並寫入 Supabase `box` table

預設裝置：

- `ab170023`
- `ab170019`
- `ab170010`

### Weather Images

- `GET /api/stored-radar`
  - 抓取最新可用雷達影像，並上傳至 Supabase Storage
- `GET /api/stored-satellite`
  - 抓取最新可用衛星雲圖，並上傳至 Supabase Storage
- `GET /api/last-3-hours`
  - 直接從 Supabase `satellite_images` table 讀取近 3 小時的雷達回波與可見光圖資料

影像資料會寫入：

- Storage bucket: `satellite-images`
- Table: `satellite_images`

### Marquees

- `GET /api/marquees`
  - 取得最近 24 小時內啟用中的跑馬燈
- `POST /api/marquees`
  - 建立跑馬燈，需要 API Key
- `DELETE /api/marquees/{marquee_id}`
  - 刪除跑馬燈，需要 API Key

可用分類：

- `rain`
- `wind`
- `pm25`

建立跑馬燈的 request body 範例：

```json
{
  "content": "今日午後有短暫陣雨，請記得攜帶雨具",
  "category": "rain"
}
```

需要驗證的路由請在 header 帶入：

```http
X-API-KEY: your-secret-key
```

## Environment Variables

請先建立 `.env`：

```env
SUPABASE_URL=your_supabase_url
SUPABASE_KEY=your_supabase_service_key
API_SECRET_KEY=your_api_secret_key
```

說明：

- `SUPABASE_URL`: Supabase 專案 URL
- `SUPABASE_KEY`: Supabase API Key
- `API_SECRET_KEY`: 跑馬燈寫入與刪除使用的 API Key

注意：

- 若缺少 `SUPABASE_URL` 或 `SUPABASE_KEY`，應用程式啟動時會直接報錯。
- 若未設定 `API_SECRET_KEY`，程式會退回預設值 `stan-default-secret`。正式環境請務必覆蓋。

## Installation

```bash
pip install -r requirements.txt
```

## Run Locally

```bash
python main.py
```

啟動後預設服務位置：

```text
http://127.0.0.1:8000
```

Swagger 文件：

```text
http://127.0.0.1:8000/docs
```

或使用 uvicorn：

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## Example Requests

取得最新感測器資料：

```bash
curl http://127.0.0.1:8000/api/sensor/latest
```

取得歷史資料：

```bash
curl "http://127.0.0.1:8000/api/sensor/history?device_id=ab170023"
```

取得近 3 小時雷達與可見光圖：

```bash
curl http://127.0.0.1:8000/api/last-3-hours
```

建立跑馬燈：

```bash
curl -X POST http://127.0.0.1:8000/api/marquees \
  -H "Content-Type: application/json" \
  -H "X-API-KEY: your-secret-key" \
  -d '{"content":"今日風勢偏強，請注意安全","category":"wind"}'
```

刪除跑馬燈：

```bash
curl -X DELETE http://127.0.0.1:8000/api/marquees/1 \
  -H "X-API-KEY: your-secret-key"
```

## Deployment

專案已包含 `vercel.json`，可直接部署到 Vercel。

部署時請同步設定以下環境變數：

- `SUPABASE_URL`
- `SUPABASE_KEY`
- `API_SECRET_KEY`

## Notes

- `main.py` 會把 `api.py` 掛載到 `/api`
- 專案目前允許所有來源的 CORS
- 天氣影像抓取失敗時，API 會回傳 `404`
- 感測器與天氣來源依賴外部服務，若外部服務異常，結果可能為空或失敗
