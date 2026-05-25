# Workstation Monitoring System (EM Model)

A real-time **workstation occupancy detection** platform that turns any RTSP / IP camera feed into actionable workforce-presence analytics using **YOLOv8** person detection, **HLS streaming**, **Server-Sent Events (SSE)**, and a **PostgreSQL** analytics backend.

---

## Table of Contents

1. [Project Overview](#project-overview)
2. [Use Cases](#use-cases)
3. [System Architecture & Flow](#system-architecture--flow)
4. [Project Structure](#project-structure)
5. [How Inference Works](#how-inference-works)
6. [Installation & Setup](#installation--setup)
7. [Running the Project](#running-the-project)
8. [API Reference](#api-reference)
9. [Database Schema](#database-schema)
10. [Docker / EC2 Deployment](#docker--ec2-deployment)
11. [Environment Variables](#environment-variables)
12. [Troubleshooting](#troubleshooting)

---

## Project Overview

The **EM (Employee Monitoring) Model** is a GPU-accelerated computer-vision micro-service that:

- Ingests live camera streams (RTSP / HTTP / file).
- Detects people in real time with **YOLOv8s**.
- Maps detections to user-defined **Workstation ROIs** (Regions of Interest) per camera.
- Tracks each workstation as `ACTIVE` or `VACANT` with a configurable grace period.
- Streams the annotated video back to the browser via **HLS**.
- Pushes live status changes to the UI via **SSE**.
- Persists daily utilization analytics and vacancy timelines into **PostgreSQL**.

It is designed as the inference backend for a larger SaaS dashboard where organisations can monitor **multiple cameras, multiple workstations**, and pull historical reports.

---

## Use Cases

| Industry | Use Case |
|---|---|
| **BPO / Call Centres** | Track seat occupancy, shrinkage, and break-time compliance per agent desk. |
| **Manufacturing** | Monitor operator presence at production stations to flag missing operators. |
| **Co-working / Offices** | Real-time desk availability and utilization reporting for facility planning. |
| **Warehousing** | Detect whether picking/packing stations are manned during shift hours. |
| **Security & Compliance** | Audit critical zones (control rooms, cash counters) for continuous human presence. |

---

## System Architecture & Flow

```
                ┌──────────────────────────────────────────────────────────┐
                │                       CLIENT (React)                     │
                │  - Draws ROIs on camera frame                            │
                │  - Plays HLS video                                       │
                │  - Subscribes to SSE for live status                     │
                └──────────┬──────────────────────────────────┬────────────┘
                           │ REST (start/stop, ROI CRUD)      │ HLS + SSE
                           ▼                                  ▼
                ┌──────────────────────────────────────────────────────────┐
                │                  FastAPI Backend (app.py)                │
                │  - /streams/*    /workstations/*   /hls/*   /events/*    │
                │  - Spawns one stream_worker() thread per camera          │
                └──────────┬───────────────────────────────────────────────┘
                           │
              ┌────────────┴────────────────────────────────┐
              ▼                                             ▼
   ┌────────────────────────┐                  ┌──────────────────────────┐
   │  WorkstationInference  │   annotated      │         FFmpeg           │
   │  (workstation_         │ ───frames──────► │  H.264 (NVENC/libx264)   │
   │   inference.py)        │                  │   → HLS segments (.ts)   │
   │  - YOLOv8s person det. │                  └────────────┬─────────────┘
   │  - ROI hit-testing     │                               │
   │  - State machine       │                               ▼
   │  - Grace period (3s)   │                  ┌──────────────────────────┐
   └────────────┬───────────┘                  │   hls_out/<stream_id>/   │
                │                              │     playlist.m3u8        │
                │ status changes               └──────────────────────────┘
                ▼
   ┌────────────────────────┐
   │   PostgreSQL (visco)   │
   │  - workstations        │
   │  - daily_analytics     │
   │  - vacancy_logs        │
   └────────────────────────┘
```

### End-to-End Request Flow

```
  ① Client → POST /workstations/save        (define ROIs once per camera)
  ② Client → POST /streams/start            (begin inference + HLS encode)
  ③ Backend → spawns stream_worker thread:
        ├── reads frames from RTSP source
        ├── runs YOLOv8 on each frame
        ├── checks centroids against ROIs
        ├── updates WorkstationState (ACTIVE / VACANT)
        ├── pipes annotated frame → FFmpeg → HLS
        ├── emits SSE event on status change
        └── flushes analytics to PostgreSQL every 10 s
  ④ Client ← GET  /hls/<stream_id>/playlist.m3u8   (video)
  ⑤ Client ← GET  /streams/events/<stream_id>      (SSE updates)
  ⑥ Client → POST /streams/stop             (graceful shutdown)
```

---

## Project Structure

```
em_model/
├── app.py                       # FastAPI service — endpoints, threading, HLS mount
├── workstation_inference.py     # YOLOv8 engine + per-workstation state machine + DB writes
├── connections/
│   └── create_tables.py         # One-time PostgreSQL schema bootstrap
├── example.py                   # Reference snippet for using WorkstationInference standalone
├── save.py                      # Auxiliary script (data/save helpers)
├── yolov8s.pt                   # YOLOv8-small weights (bundled)
├── requirements.txt             # Python deps (CUDA 12.1 PyTorch)
├── Dockerfile                   # CUDA 12.1 + ffmpeg base image
└── .github/workflows/deploy.yml # Auto-deploy to EC2 on push to main
```

---

## How Inference Works

1. `stream_worker()` instantiates a `WorkstationInference` object per stream (one thread per camera).
2. Workstation ROIs for `(org_id, cam_id)` are loaded from PostgreSQL via `load_workstations_from_db()`.
3. For every frame:
   - **YOLOv8s** runs on GPU (class `0` = person, confidence ≥ `0.4`).
   - Each detection's **centroid** is computed and tested against all ROI rectangles.
   - The ROI's `WorkstationState` is updated (`occupied`, `last_seen_time`, etc.).
4. A **grace period** of `missing_threshold = 3 s` prevents flicker — a workstation only transitions to `VACANT` after 3 seconds of no detection.
5. The annotated frame (with ROI boxes + status labels) is written to FFmpeg's stdin, encoded as **H.264** (NVENC on GPU EC2, libx264 elsewhere) and segmented into an **HLS playlist** under `hls_out/<stream_id>/`.
6. On any status transition, a JSON event is pushed to all subscribers of `/streams/events/<stream_id>`.
7. Every **10 seconds**, accumulated metrics are upserted into `workstation_daily_analytics`; each completed vacancy interval becomes a row in `workstation_vacancy_logs`.

---

## Installation & Setup

### Prerequisites

- **Python 3.10+**
- **NVIDIA GPU** with **CUDA 12.1** drivers (for real-time inference). CPU fallback works but is slow.
- **FFmpeg** installed and on `PATH` (the Docker image installs this automatically).
- **PostgreSQL 13+** reachable from the host.

### 1. Clone the repository

```bash
git clone <repo-url> em_model
cd em_model
```

### 2. Create a virtual environment

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

> `requirements.txt` pins **torch 2.5.1 + cu121**. If you do not have a CUDA 12.1 GPU, install the CPU build of PyTorch separately before running `pip install -r requirements.txt`.

### 4. Configure environment variables

Create a `.env` file in the project root:

```env
DB_HOST=localhost
DB_PORT=5432
DB_NAME=visco
DB_USER=postgres
DB_PASSWORD=your_password
```

### 5. Initialize the database (one-time)

```bash
python connections/create_tables.py
```

Expected output:
```
🛠️  Initializing Fresh Database Tables...
✅ All tables created successfully.
```

---

## Running the Project

### Local development server

```bash
uvicorn app:app --reload --host 0.0.0.0 --port 8008
```

Open the auto-generated docs at: <http://localhost:8008/docs>

### Quick smoke test

```bash
# 1. Save ROIs for camera 101 in org 1
curl -X POST http://localhost:8008/workstations/save \
  -H "Content-Type: application/json" \
  -d '{
        "org_id": 1, "cam_id": 101,
        "workstations": [
          {"name": "Desk-A", "x1": 0.10, "y1": 0.20, "x2": 0.40, "y2": 0.80},
          {"name": "Desk-B", "x1": 0.55, "y1": 0.20, "x2": 0.85, "y2": 0.80}
        ]
      }'

# 2. Start a stream
curl -X POST http://localhost:8008/streams/start \
  -H "Content-Type: application/json" \
  -d '{"source": "rtsp://user:pass@camera-ip/stream", "org_id": 1, "cam_id": 101, "user_id": 1}'

# 3. Watch the HLS playlist (browser or VLC)
#    http://localhost:8008/hls/<stream_id>/playlist.m3u8
```

---

## API Reference

### Stream Management

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/streams/start` | Start HLS stream with inference. Body: `{source, org_id, cam_id, user_id, use_nvenc}` |
| `POST` | `/streams/stop` | Stop a running stream. Body: `{stream_id}` |
| `GET`  | `/streams/list` | List all active streams |
| `GET`  | `/streams/events/{stream_id}` | SSE channel — pushes JSON on workstation status change |

### Workstation ROI Management

| Method | Endpoint | Description |
|---|---|---|
| `POST`   | `/workstations/save` | Upsert ROI boxes for a camera. Body: `{org_id, cam_id, workstations: [{name, x1, y1, x2, y2}]}` |
| `GET`    | `/workstations/check?org_id=X&cam_id=Y` | Returns whether ROIs already exist for a camera |
| `DELETE` | `/workstations/delete` | Delete a single ROI by name, or **all** ROIs for the camera. Body: `{org_id, cam_id, name?}` |

### Misc

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Health check |
| `GET` | `/hls/{stream_id}/playlist.m3u8` | HLS playlist (served from `hls_out/`) |

---

## Database Schema

Three tables are created by `connections/create_tables.py`:

| Table | Purpose | Unique Key |
|---|---|---|
| `workstations` | ROI definitions per camera `(name, x1, y1, x2, y2)` | `(org_id, cam_id, name)` |
| `workstation_daily_analytics` | Daily roll-up: `active_seconds`, `vacant_seconds`, `utilization_percent`, `missing_count` | `(org_id, cam_id, workstation_name, analytics_date)` |
| `workstation_vacancy_logs` | Timeline of vacancy intervals: `start_time`, `end_time`, `duration_seconds` | — |

Coordinates are stored **normalized** (`0.0–1.0`) so ROIs remain resolution-independent.

---

## Docker / EC2 Deployment

A CI/CD pipeline (`.github/workflows/deploy.yml`) auto-builds and ships the image to a GPU-enabled EC2 instance on every push to `main`.

### Image highlights

- Base: `nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04`
- Includes `ffmpeg`, OpenCV system libs, and Python 3.10.
- Exposes port `8008`.

### Manual run on EC2

```bash
# Build
docker build -t em_model .

# Run with GPU + env file + AWS creds
docker run -d --name em_container \
  --gpus all \
  --env-file /home/ubuntu/em_model/.env \
  -v /home/ubuntu/.aws:/root/.aws:ro \
  -p 8008:8008 \
  em_model
```

Check logs:
```bash
docker logs -f em_container
```

---

## Environment Variables

| Variable | Description | Example |
|---|---|---|
| `DB_HOST` | PostgreSQL host | `db.internal` |
| `DB_PORT` | PostgreSQL port | `5432` |
| `DB_NAME` | Database name | `visco` |
| `DB_USER` | DB user | `visco_app` |
| `DB_PASSWORD` | DB password | `••••••` |

> The `.env` file must **never** be committed to git.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `CUDA not available` | Wrong PyTorch wheel / no NVIDIA driver | Reinstall the `+cu121` build or run on CPU |
| HLS playlist 404 | Stream not started or `stream_id` wrong | `GET /streams/list` to verify; check FFmpeg logs |
| SSE never fires | Workstation state never changes | Confirm ROIs cover where people sit; lower `confidence_threshold` |
| DB timeout | `.env` missing or wrong host | Re-check env vars; ensure security group allows port 5432 |
| `nvenc` errors locally | No NVIDIA encoder on dev box | Send `"use_nvenc": false` in `/streams/start` |

---

## License & Credits

- Person detection: [Ultralytics YOLOv8](https://github.com/ultralytics/ultralytics)
- Web framework: [FastAPI](https://fastapi.tiangolo.com/)
- Video pipeline: [FFmpeg](https://ffmpeg.org/)
