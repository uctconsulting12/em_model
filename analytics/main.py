import os
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional
from datetime import date, time
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(
    title="Workstation Occupancy Analytics API",
    version="1.0.0"
)

# ── Models ────────────────────────────────────────────────────────────────────

class DailyAttendanceResponse(BaseModel):
    workstation_name: str
    analytics_date: date
    arrival_time: Optional[time]
    exit_time: Optional[time]
    total_hours: Optional[float]

class UtilizationResponse(BaseModel):
    analytics_date: date
    avg_office_utilization: float

class TopDeskResponse(BaseModel):
    workstation_name: str
    avg_utilization: float

class BreakMetricsResponse(BaseModel):
    workstation_name: str
    total_times_left_desk: int
    total_minutes_away: float

class ShiftMetricsResponse(BaseModel):
    workstation_name: str
    first_seen_time: Optional[time]
    last_present_time: Optional[time]
    total_hours_on_site: Optional[float]

class DailyKPIResponse(BaseModel):
    active_desks_count: int
    total_office_active_hours: float

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = None
    try:
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "localhost"),
            port=os.getenv("DB_PORT", "5432"),
            dbname=os.getenv("DB_NAME", "postgres"),
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD", "postgres"),
        )
        yield conn
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")
    finally:
        if conn:
            conn.close()

def query(conn, sql: str, params: tuple = ()):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchall()

def query_one(conn, sql: str, params: tuple = ()):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchone()

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/api/v1/analytics/summary/today", response_model=DailyKPIResponse)
def get_daily_kpis(org_id: int = 1, conn=Depends(get_db)):
    """Total active desks and hours worked today."""
    row = query_one(conn, """
        SELECT
            COUNT(workstation_name)                              AS active_desks_count,
            ROUND((SUM(active_seconds) / 3600.0)::numeric, 2)   AS total_office_active_hours
        FROM workstation_daily_analytics
        WHERE analytics_date = CURRENT_DATE
          AND org_id = %s
          AND active_seconds > 0;
    """, (org_id,))
    if not row:
        raise HTTPException(status_code=404, detail="No data for today.")
    return row


@app.get("/api/v1/analytics/utilization/history", response_model=List[UtilizationResponse])
def get_utilization_history(org_id: int = 1, limit: int = 30, conn=Depends(get_db)):
    """Average office utilization % day-over-day."""
    return query(conn, """
        SELECT
            analytics_date,
            ROUND(AVG(utilization_percent)::numeric, 2) AS avg_office_utilization
        FROM workstation_daily_analytics
        WHERE org_id = %s
        GROUP BY analytics_date
        ORDER BY analytics_date DESC
        LIMIT %s;
    """, (org_id, limit))


@app.get("/api/v1/analytics/workstations/top", response_model=List[TopDeskResponse])
def get_top_workstations(days: int = 7, limit: int = 5, conn=Depends(get_db)):
    """Most utilized desks over the last N days."""
    return query(conn, """
        SELECT
            workstation_name,
            ROUND(AVG(utilization_percent)::numeric, 2) AS avg_utilization
        FROM workstation_daily_analytics
        WHERE analytics_date >= CURRENT_DATE - (%s || ' days')::interval
        GROUP BY workstation_name
        ORDER BY avg_utilization DESC
        LIMIT %s;
    """, (days, limit))


@app.get("/api/v1/analytics/workstations/breaks/today", response_model=List[BreakMetricsResponse])
def get_break_metrics(conn=Depends(get_db)):
    """Break counts and time away from desk today."""
    return query(conn, """
        SELECT
            workstation_name,
            SUM(missing_count)                                    AS total_times_left_desk,
            ROUND((SUM(missing_duration) / 60.0)::numeric, 2)    AS total_minutes_away
        FROM workstation_daily_analytics
        WHERE analytics_date = CURRENT_DATE
        GROUP BY workstation_name
        ORDER BY total_minutes_away DESC;
    """)


@app.get("/api/v1/analytics/workstations/shifts/today", response_model=List[ShiftMetricsResponse])
def get_shift_metrics(conn=Depends(get_db)):
    """First arrival and last departure times today."""
    return query(conn, """
        SELECT
            workstation_name,
            first_seen_time,
            last_present_time,
            ROUND((EXTRACT(EPOCH FROM (last_present_time - first_seen_time)) / 3600)::numeric, 2)
                AS total_hours_on_site
        FROM workstation_daily_analytics
        WHERE analytics_date = CURRENT_DATE
        ORDER BY first_seen_time ASC;
    """)


@app.get(
    "/api/v1/analytics/workstations/{desk_name}/attendance",
    response_model=DailyAttendanceResponse,
)
def get_desk_attendance(
    desk_name: str,
    query_date: date = Query(default_factory=date.today, description="YYYY-MM-DD"),
    org_id: int = 1,
    conn=Depends(get_db),
):
    """Arrival time, exit time, and total hours for a specific desk on a given date."""
    row = query_one(conn, """
        SELECT
            workstation_name,
            analytics_date,
            first_seen_time                                                          AS arrival_time,
            last_present_time                                                        AS exit_time,
            ROUND((EXTRACT(EPOCH FROM (last_present_time - first_seen_time)) / 3600.0)::numeric, 2)
                AS total_hours
        FROM workstation_daily_analytics
        WHERE org_id = %s
          AND workstation_name = %s
          AND analytics_date = %s;
    """, (org_id, desk_name, query_date))
    if not row:
        raise HTTPException(status_code=404, detail=f"No data for {desk_name} on {query_date}.")
    return row
