# Workstation Monitoring System (EM Model)

Real-time workstation occupancy detection using **YOLOv8** person detection with **HLS streaming**, **SSE events**, and **PostgreSQL** analytics persistence.

## Architecture

```
┌──────────────┐     POST /streams/start       ┌─────────────────────┐
│ React Client │ ──────────────────────────►   │   FastAPI (app.py)  │
│              │ ◄──── HLS (.m3u8/.ts) ─────── │                     │
│              │ ◄──── SSE (events) ────────── │   stream_worker()   │
└──────────────┘                               │     per stream      │
                                               └────────┬────────────┘
                                                        │
                                    ┌───────────────────┤
                                    ▼                    ▼
                         ┌──────────────────┐   ┌──────────────┐
                         │ WorkstationInfer │   │   FFmpeg      │
                         │ (YOLOv8 + ROIs)  │   │  (HLS encode) │
                         └────────┬─────────┘   └──────────────┘
                                  │
                                  ▼
                         ┌──────────────────┐
                         │   PostgreSQL     │
                         │  (workstations,  │
                         │   analytics,     │
                         │   vacancy_logs)  │
                         └──────────────────┘
```

## Key Files

| File | Purpose |
|---|---|
| `app.py` | FastAPI backend — stream management, ROI CRUD, SSE events, HLS serving |
| `workstation_inference.py` | YOLOv8 inference engine — person detection, workstation state tracking, DB updates |
| `connections/create_tables.py` | PostgreSQL schema initialization (run once) |
| `Dockerfile` | CUDA 12.1 + ffmpeg container for EC2 GPU deployment |
| `.github/workflows/deploy.yml` | CI/CD — auto-deploys to EC2 on push to `main` |

## API Endpoints

### Stream Management

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/streams/start` | Start HLS stream with inference. Body: `{source, org_id, cam_id, user_id, use_nvenc}` |
| `POST` | `/streams/stop` | Stop a running stream. Body: `{stream_id}` |
| `GET` | `/streams/list` | List all active streams |
| `GET` | `/streams/events/{stream_id}` | SSE endpoint — pushes JSON on workstation status change |

### Workstation ROI Management

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/workstations/save` | Upsert ROI boxes for a cam_id. Body: `{org_id, cam_id, workstations: [{name, x1, y1, x2, y2}]}` |
| `GET` | `/workstations/check?org_id=X&cam_id=Y` | Check if ROIs exist for a camera |
| `DELETE` | `/workstations/delete` | Delete ROI by name or all for a cam_id. Body: `{org_id, cam_id, name?}` |

### Other

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Health check |
| `GET` | `/hls/{stream_id}/playlist.m3u8` | HLS playlist (static file mount) |

## Database Schema

Three tables in PostgreSQL (see `connections/create_tables.py`):

- **`workstations`** — ROI definitions per camera: `(org_id, cam_id, name, x1, y1, x2, y2)`. Unique on `(org_id, cam_id, name)`.
- **`workstation_daily_analytics`** — Per-workstation per-day metrics: active/vacant seconds, utilization %, missing count. Unique on `(org_id, cam_id, workstation_name, analytics_date)`.
- **`workstation_vacancy_logs`** — Timeline of vacancy events with start/end times and durations.

## How Inference Works

1. `stream_worker()` creates a `WorkstationInference` instance per stream
2. ROIs are loaded from DB via `load_workstations_from_db(org_id, cam_id)`
3. Each frame: YOLOv8 detects person centroids → checks which ROIs contain a centroid → updates workstation state (`ACTIVE`/`VACANT`)
4. State transitions have a **grace period** (`missing_threshold=3s`) before marking vacant
5. Annotated frames are piped to FFmpeg for HLS encoding
6. Status changes trigger SSE events to connected clients
7. Analytics are persisted to PostgreSQL every 10 seconds

## Environment Variables

Create a `.env` file (not committed to git):

```env
DB_HOST=<postgres-host>
DB_PORT=5432
DB_NAME=visco
DB_USER=<username>
DB_PASSWORD=<password>
```

## Local Development

```bash
# Install dependencies (requires CUDA 12.1 for GPU inference)
pip install -r requirements.txt

# Initialize database tables (one-time)
python connections/create_tables.py

# Run the server
uvicorn app:app --reload --port 8008
```

## Docker Deployment (EC2)

The app deploys automatically via GitHub Actions on push to `main`.

**Manual setup (one-time on EC2):**
```bash
# Create .env on EC2
nano ~/em_model/.env

# Verify container is running
sudo docker ps
sudo docker logs em_container
```

**Container runs with:**
- `--gpus all` for CUDA acceleration
- Port `8008` exposed
- `.env` loaded via `--env-file`
- AWS credentials mounted from `/home/ubuntu/.aws`

## Model

Uses **YOLOv8s** for person detection (class 0 only). The model file is committed to the repo and baked into the Docker image.
