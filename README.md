# VIP Proactive Monitoring

ระบบ monitoring แบบ proactive สำหรับตรวจสอบสถานะ fibre ทุกหมายเลขใน `fibre_list.txt`
โดยรวบรวมข้อมูลจาก **3 sources** พร้อมกัน และสรุป overall status ลงไฟล์ `output/summary.json`

---

## Architecture

```mermaid
graph TD
    main["main.py\n(Orchestrator)"] -->|"asyncio.gather"| A["modules/airnet\n(Playwright scraper)"]
    main -->|"asyncio.gather"| B["modules/onesense\n(REST API — future)"]
    main -->|"asyncio.gather"| C["modules/npaw\n(REST API — future)"]

    A -->|"ModuleResult"| AGG["Aggregator\n(overall status)"]
    B -->|"ModuleResult"| AGG
    C -->|"ModuleResult"| AGG

    AGG --> SUM["output/summary.json"]
    A --> DETAIL["output/result_XXXX.json\n(detailed per fibre)"]

    IN["fibre_list.txt"] --> main
```

**Overall status logic** (per fibre):

| Active module results | Overall status |
|---|---|
| ทุก module คืน `normal` | `normal` |
| มี module ไหนคืน `abnormal` | `abnormal` |
| มี module ไหนคืน `error` | `error` |
| modules ที่คืน `N/A` ไม่นับรวม | — |
| ทุก module คืน `N/A` | `unknown` |

---

## Project Structure

```
vip-proactive-monitoring/
├── main.py                          # Orchestrator — อ่าน fibre list, รัน 3 modules, สรุป summary
├── fibre_list.txt                   # Input: หมายเลข fibre ทีละบรรทัด
├── .env                             # Credentials & config (git-ignored)
├── .env.example                     # Template สำหรับสร้าง .env
├── requirements.txt                 # Python dependencies
├── common/
│   ├── __init__.py
│   └── models.py                    # ModuleResult dataclass (shared by all modules)
├── modules/
│   ├── __init__.py
│   ├── airnet/                      # Playwright-based web scraper
│   │   ├── __init__.py
│   │   ├── checker.py               # check(fibre_id) -> ModuleResult (entry point)
│   │   ├── auth.py                  # Login automation
│   │   ├── config.py                # .env config loader
│   │   ├── extractor.py             # Historical Usage table extractor
│   │   ├── query.py                 # Customer query & status check
│   │   └── utils.py                 # Retry decorator & logging setup
│   ├── onesense/                    # OneSense API (stub — future)
│   │   ├── __init__.py
│   │   └── checker.py               # check(fibre_id) -> ModuleResult (returns N/A)
│   └── npaw/                        # NPAW API (stub — future)
│       ├── __init__.py
│       └── checker.py               # check(fibre_id) -> ModuleResult (returns N/A)
└── output/
    ├── summary.json                 # Main output: overall status ทุก fibre
    └── result_XXXX.json             # Airnet detailed output ต่อ fibre
```

---

## Prerequisites

- **Python** 3.10 or higher
- **Playwright** Chromium browser binaries

---

## Installation

1. **(Optional) Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   # Windows (PowerShell)
   .venv\Scripts\Activate.ps1
   # Linux/macOS
   source .venv/bin/activate
   ```

2. **Install Python dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Install Playwright browser binaries:**
   ```bash
   playwright install chromium
   ```

---

## Configuration

Copy `.env.example` to `.env` แล้วกรอก credentials:

```bash
cp .env.example .env
```

```env
# === Airnet (web scraper) ===
BASE_URL=https://your-airnet-portal/AirnetWeb
USERNAME=your_username
PASSWORD=your_password

# === OneSense (future REST API) ===
# ONESENSE_API_URL=
# ONESENSE_API_KEY=

# === NPAW (future REST API) ===
# NPAW_API_URL=
# NPAW_API_KEY=
```

แก้ไข `fibre_list.txt` เพื่อกำหนด fibre IDs ที่ต้องการตรวจสอบ (10 หลัก ทีละบรรทัด):

```
8800021298
8800052568
8801815340
```

---

## Usage

รัน main orchestrator (ตรวจสอบทุก fibre ใน fibre_list.txt):

```bash
python main.py
```

Console output จะแสดงตารางสรุปสถานะ:

```
========================================================================
  VIP Proactive Monitoring — Summary
========================================================================
  Started : 2026-09-01T16:30:00+07:00
  Finished: 2026-09-01T16:35:42+07:00
  Fibres  : 3

  Fibre ID       Overall      Airnet       OneSense     NPAW
  ──────────────────────────────────────────────────────────────
  8800021298     normal       normal       N/A          N/A
  8800052568     abnormal     abnormal     N/A          N/A
  8801815340     normal       normal       N/A          N/A
========================================================================
  normal: 2  abnormal: 1  error: 0  unknown: 0
========================================================================
```

---

## Output Format

### `output/summary.json` — Main summary (สร้างโดย main.py)

```json
{
  "started_at": "2026-09-01T16:30:00+07:00",
  "finished_at": "2026-09-01T16:35:42+07:00",
  "fibre_count": 3,
  "status_counts": { "normal": 2, "abnormal": 1, "error": 0, "unknown": 0 },
  "fibres": [
    {
      "fibre_id": "8800021298",
      "overall_status": "normal",
      "modules": {
        "airnet":   { "status": "normal", "details": { "online_status": "Online", "row_count": 42 } },
        "onesense": { "status": "N/A",    "details": { "message": "Not implemented yet" } },
        "npaw":     { "status": "N/A",    "details": { "message": "Not implemented yet" } }
      }
    }
  ]
}
```

### `output/result_XXXX.json` — Airnet detailed output (per fibre)

```json
{
  "query_number": "8800021298",
  "online_status": "Online",
  "scraped_at": "2026-09-01T16:30:00+07:00",
  "row_count": 42,
  "data": [
    {
      "Customer ID": "88XXXXX298",
      "Service": "INTERNET",
      "IP Address": "100.xxx.xxx.78",
      "Online Time": "01/09/2026 08:00",
      "Download": "6.35 GB",
      "Upload": "547.82 MB"
    }
  ]
}
```

---

## Module Status Values

| Status | ความหมาย |
|---|---|
| `normal` | บริการปกติ |
| `abnormal` | บริการผิดปกติ / offline |
| `error` | module ดึงข้อมูลไม่สำเร็จ |
| `N/A` | module ยังไม่ได้ implement |
| `unknown` | ทุก module คืน N/A (ไม่มีข้อมูล) |

---

## Adding a New Module

เมื่อ OneSense หรือ NPAW API พร้อม ให้แก้ไข `modules/<name>/checker.py`:

1. เพิ่ม API credentials ใน `.env`
2. แก้ `check()` function ให้ call API จริง (ใช้ `httpx` ที่ติดตั้งไว้แล้ว)
3. Map response → `ModuleResult` โดยใช้ status `normal`/`abnormal`/`error`
4. main.py ไม่ต้องแก้ไขใดๆ เลย
