import os
import uuid
import shutil
import threading
import subprocess
import logging
import time
import json
import queue
from typing import Optional, Dict, List

import psycopg2

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

from workstation_inference import WorkstationInference, DatabaseConfig

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- HLS output ----------------
HLS_ROOT = "hls_out"
os.makedirs(HLS_ROOT, exist_ok=True)
app.mount("/hls", StaticFiles(directory=HLS_ROOT), name="hls")


# Keep track of running streams (1 thread per stream)
streams: Dict[str, dict] = {}
streams_lock = threading.Lock()


class StartRequest(BaseModel):
    source: str
    org_id: int |  None = None
    cam_id: int | None = None
    user_id: int | None = None
    use_nvenc: bool = False


class StopRequest(BaseModel):
    stream_id: str


class WorkstationROI(BaseModel):
    name: str
    x1: float
    y1: float
    x2: float
    y2: float


class SaveWorkstationsRequest(BaseModel):
    org_id: int
    cam_id: int
    workstations: List[WorkstationROI]


class DeleteWorkstationRequest(BaseModel):
    org_id: int
    cam_id: int
    name: str | None = None  # If None, deletes ALL for this cam_id


def _get_db_connection():
    """Create a DB connection from env vars."""
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )


def _ffmpeg_exe() -> str:
    """Return ffmpeg executable path."""
    # You might want to use imageio_ffmpeg if available, 
    # but for now we default to system ffmpeg
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"


def stream_worker(stream_id: str, source: str, org_id: int, cam_id: int, stop_event: threading.Event, use_nvenc: bool):
    """
    Worker thread that runs inference and pipes frames to FFMPEG for HLS streaming.
    """
    out_dir = os.path.join(HLS_ROOT, stream_id)
    os.makedirs(out_dir, exist_ok=True)
    out_m3u8 = os.path.join(out_dir, "playlist.m3u8")

    # Initialize Inference Engine
    # Note: Database updates will run automatically if enabled in WorkstationInference
    db_config = DatabaseConfig(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )

    try:
        inference = WorkstationInference(
            model_path="yolov8s.pt", # Ensure this path is correct
            db_config=db_config,
            org_id=org_id,
            cam_id=cam_id,
            auto_update_db=True
        )
    except Exception as e:
        logger.error(f"Failed to initialize inference: {e}")
        return

    # Open Video Source
    # Handle integer sources (webcam) vs string sources (files/RTSP)
    video_source = int(source) if source.isdigit() else source
    cap = cv2.VideoCapture(video_source)
    
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1) # Minimize buffer for lower latency
    
    if not cap.isOpened():
        logger.error(f"stream_id={stream_id} failed to open source={source}")
        return

    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1280)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 720)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    if fps <= 1:
        fps = 25.0

    # FFmpeg Settings
    vcodec = "libx264"
    if use_nvenc:
        vcodec = "h264_nvenc"

    bitrate_k = 1024
    hls_time = 4
    hls_list_size = 100
    gop = max(25, int(float(fps) * 2))

    ffmpeg_cmd = [
        _ffmpeg_exe(),
        "-y",
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
        "-s", f"{w}x{h}",
        "-r", str(fps),
        "-i", "-",
        "-c:v", vcodec,
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-g", str(gop),
        "-keyint_min", str(gop),
        "-sc_threshold", "0",
        "-crf", "28",
        "-maxrate", f"{bitrate_k}k",
        "-bufsize", f"{bitrate_k * 2}k",
        "-f", "hls",
        "-hls_time", str(hls_time),
        "-hls_list_size", str(hls_list_size),
        "-hls_flags", "delete_segments+append_list",
        out_m3u8,
    ]

    proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE)
    
    with streams_lock:
        if stream_id in streams:
            streams[stream_id]["proc"] = proc
            # Ensure events queue exists
            if "events" not in streams[stream_id]:
                streams[stream_id]["events"] = queue.Queue()

    logger.info(f"Stream {stream_id} started. Output: {out_m3u8}")

    # Track previous statuses for change detection
    prev_statuses: Dict[str, str] = {}
    frame_index = 0

    try:
        while not stop_event.is_set():
            ret, frame = cap.read()
            if not ret:
                logger.info(f"Stream {stream_id} source finished")
                break

            frame_index += 1

            # Run Inference & Annotation
            annotated_frame = inference.process_frame(frame)
            
            # Detect status changes and push SSE events
            current_statuses = {name: ws.status for name, ws in inference.workstations.items()}
            status_changed = current_statuses != prev_statuses

            if status_changed and inference.workstations:
                prev_statuses = current_statuses.copy()

                # Build event payload with all workstations
                ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
                metrics = inference.get_metrics()
                event_payload = {
                    "stream_id": stream_id,
                    "frame_num": frame_index,
                    "timestamp": ts,
                    "workstations": metrics
                }

                # Push to SSE queue
                with streams_lock:
                    entry = streams.get(stream_id)
                    if entry:
                        events_q = entry.get("events")
                        if events_q is not None:
                            try:
                                events_q.put_nowait(event_payload)
                            except queue.Full:
                                pass  # Drop if back-pressured

            # Resize if needed (though process_frame usually preserves size)
            if annotated_frame.shape[:2] != (h, w):
                annotated_frame = cv2.resize(annotated_frame, (w, h))

            # Pipe to FFmpeg
            try:
                if proc.stdin:
                    proc.stdin.write(annotated_frame.tobytes())
            except BrokenPipeError:
                logger.error(f"Stream {stream_id} broken pipe")
                break
                
    except Exception as e:
        logger.error(f"Stream {stream_id} error: {e}")
        
    finally:
        logger.info(f"Stopping stream {stream_id}")
        cap.release()
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=2)
        except Exception as e:
            logger.error(f"Error closing ffmpeg for {stream_id}: {e}")


def _find_active_stream_for_user(user_id: int | None) -> tuple[str, str] | None:
    """
    Return (stream_id, hls_url) if this user already has a running stream.
    """
    if user_id is None:
        return None

    with streams_lock:
        for sid, entry in streams.items():
            stop_event = entry.get("stop")
            if entry.get("user_id") == user_id and stop_event and not stop_event.is_set():
                # port 8000 is hardcoded for now as per previous testing
                return sid, f"/hls/{sid}/playlist.m3u8"
    return None


@app.post("/streams/start")
def start_stream(req: StartRequest):
    """Start a new HLS stream from the given source."""
    
    # Check for existing stream for this user
    existing = _find_active_stream_for_user(req.user_id)
    if existing:
        sid, hls_url = existing
        return {
            "stream_id": sid,
            "hls_url": hls_url,
            "status": "reused"
        }

    stream_id = str(uuid.uuid4())
    stop_event = threading.Event()

    # Pre-register stream entry
    with streams_lock:
        streams[stream_id] = {
            "stop": stop_event,
            "thread": None,
            "proc": None,
            "events": queue.Queue(),
            "source": req.source,
            "org_id": req.org_id,
            "cam_id": req.cam_id,
            "user_id": req.user_id
        }

    t = threading.Thread(
        target=stream_worker,
        args=(stream_id, req.source, req.org_id, req.cam_id, stop_event, req.use_nvenc),
        daemon=True,
    )
    
    with streams_lock:
        streams[stream_id]["thread"] = t
    
    t.start()

    return {
        "stream_id": stream_id,
        "hls_url": f"/hls/{stream_id}/playlist.m3u8",
        "status": "started"
    }


@app.post("/streams/stop")
def stop_stream(req: StopRequest):
    """Stop a running stream and clean up."""
    stream_id = req.stream_id
    
    with streams_lock:
        entry = streams.get(stream_id)
        if not entry:
            raise HTTPException(status_code=404, detail="Stream not found")
            
        # Signal stop
        entry["stop"].set()
        
    # Wait for thread to finish (max 5s)
    t = entry.get("thread")
    if t and t.is_alive():
        t.join(timeout=5)
        
    # Clean up files
    dir_path = os.path.join(HLS_ROOT, stream_id)
    if os.path.exists(dir_path):
        try:
            shutil.rmtree(dir_path)
        except Exception as e:
            logger.error(f"Failed to cleanup {dir_path}: {e}")

    with streams_lock:
        streams.pop(stream_id, None)

    return {"status": "stopped", "stream_id": stream_id}


@app.get("/streams/list")
def list_streams():
    """List all active streams."""
    with streams_lock:
        # Return summary of active streams
        active = []
        for sid, data in streams.items():
            active.append({
                "stream_id": sid,
                "source": data.get("source"),
                "org_id": data.get("org_id"),
                "cam_id": data.get("cam_id"),
                "hls_url": f"/hls/{sid}/playlist.m3u8" # Changed to local host for testing
            })
    return {"streams": active}


@app.get("/streams/events/{stream_id}")
def stream_events(stream_id: str):
    """
    SSE endpoint for workstation detection events.
    Pushes all workstations' state whenever any status changes.
    """

    def event_generator():
        while True:
            with streams_lock:
                entry = streams.get(stream_id)
                if not entry:
                    break
                q: queue.Queue | None = entry.get("events")

            if q is None:
                time.sleep(0.5)
                continue

            try:
                event = q.get(timeout=1.0)
            except queue.Empty:
                continue

            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ============== Workstation ROI Management ==============


@app.post("/workstations/save")
def save_workstations(req: SaveWorkstationsRequest):
    """
    Upsert ROI boxes for a specific org_id + cam_id.
    Existing workstations with the same name are updated; new ones are inserted.
    """
    conn = _get_db_connection()
    cur = conn.cursor()

    try:
        # Check if workstations already exist for this camera
        cur.execute("""
            SELECT COUNT(*) FROM workstations
            WHERE org_id = %s AND cam_id = %s
        """, (req.org_id, req.cam_id))
        existing_count = cur.fetchone()[0]

        if existing_count > 0:
            raise HTTPException(
                status_code=409,
                detail=f"Workstations already exist for cam_id={req.cam_id}. "
                       f"Delete existing ROIs first before saving new ones."
            )

        for ws in req.workstations:
            # Frontend sends (x, y, width, height) — convert to corner coords
            save_x1 = ws.x1
            save_y1 = ws.y1
            save_x2 = ws.x1 + ws.x2  # x + width
            save_y2 = ws.y1 + ws.y2  # y + height

            cur.execute("""
                INSERT INTO workstations (org_id, cam_id, name, x1, y1, x2, y2)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (req.org_id, req.cam_id, ws.name, save_x1, save_y1, save_x2, save_y2))

        conn.commit()
        return {
            "status": "saved",
            "org_id": req.org_id,
            "cam_id": req.cam_id,
            "count": len(req.workstations)
        }
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


@app.get("/workstations/check")
def check_workstations(
    org_id: int = Query(...),
    cam_id: int = Query(...)
):
    """
    Check if ROIs exist for a given org_id + cam_id.
    Returns the list of workstations if they exist.
    """
    conn = _get_db_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT name, x1, y1, x2, y2
            FROM workstations
            WHERE org_id = %s AND cam_id = %s
            ORDER BY name
        """, (org_id, cam_id))

        rows = cur.fetchall()
        workstations = [
            {"name": r[0], "x1": r[1], "y1": r[2], "x2": r[3], "y2": r[4]}
            for r in rows
        ]

        return {
            "has_workstations": len(workstations) > 0,
            "count": len(workstations),
            "org_id": org_id,
            "cam_id": cam_id,
            "workstations": workstations
        }
    finally:
        cur.close()
        conn.close()


@app.delete("/workstations/delete")
def delete_workstation(req: DeleteWorkstationRequest):
    """
    Delete a specific workstation by name, or ALL workstations for a cam_id if name is None.
    """
    conn = _get_db_connection()
    cur = conn.cursor()

    try:
        if req.name:
            cur.execute("""
                DELETE FROM workstations
                WHERE org_id = %s AND cam_id = %s AND name = %s
            """, (req.org_id, req.cam_id, req.name))
        else:
            cur.execute("""
                DELETE FROM workstations
                WHERE org_id = %s AND cam_id = %s
            """, (req.org_id, req.cam_id))

        deleted = cur.rowcount
        conn.commit()

        if deleted == 0:
            raise HTTPException(status_code=404, detail="No matching workstations found")

        return {
            "status": "deleted",
            "org_id": req.org_id,
            "cam_id": req.cam_id,
            "deleted_count": deleted
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


@app.get("/")
def health_check():
    return {"status": "ok", "service": "workstation-monitoring"}
