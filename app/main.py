"""
Employee Attendance & Analytics API - Complete Implementation

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (read from environment, with local .env fallback)
"""
import math
import os
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

# --------------------------------------------------------------------------- #
# Database Connection & Time Zone Configuration
# --------------------------------------------------------------------------- #
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MS = 100000000000
MAX_EPOCH_MS = 4102444800000


def ensure_indexes():
    """Create all required indexes idempotently at startup."""
    try:
        # Unique business constraint for employees
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        # Fast filtering and sorting by department and joined_on for headcount & summaries
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
        db.employees.create_index([("joined_on", ASCENDING)])

        # Unique business constraint for attendance: at most one record per (emp_code, date)
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
        )
        # Fast listing & range filters: date descending, emp_code ascending
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        )
        # Fast punch-out & employee monthly analytics lookup
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("punch_in", DESCENDING)]
        )
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)]
        )
        # Fast late leaderboard index: filter by date range and late_minutes > 0
        db.attendance_logs.create_index(
            [("date", ASCENDING), ("late_minutes", ASCENDING)]
        )
    except Exception as e:
        # If DB is temporarily unreachable during module import, lifespan will retry
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Rounding & Conversion Helpers
# --------------------------------------------------------------------------- #
def round_half_up(val: float, decimals: int = 2) -> float:
    """R8: Round half-up using Decimal (not banker's rounding)."""
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("10") ** (-decimals)
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to epoch milliseconds integer."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_utc_dt(ms: int) -> datetime:
    """Convert epoch milliseconds to UTC datetime, truncated to whole seconds (R1)."""
    # Truncate to whole seconds per R1
    truncated_sec = ms // 1000
    return datetime.fromtimestamp(truncated_sec, tz=timezone.utc)


def now_utc_truncated() -> datetime:
    """Return current UTC datetime truncated to whole seconds."""
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=0)


def parse_time_str(time_str: str) -> tuple[int, int]:
    parts = time_str.split(":")
    return int(parts[0]), int(parts[1])


# --------------------------------------------------------------------------- #
# Business Rule Calculations (R1 - R5, R7)
# --------------------------------------------------------------------------- #
def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date is the IST calendar date of punch_in.
    Exception: overnight shifts (shift_end <= shift_start):
    a punch-in whose IST time is strictly earlier than shift_end belongs to the
    shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start

    if is_overnight:
        end_h, end_m = parse_time_str(shift_end)
        punch_time = punch_in_ist.time()
        shift_end_time = time(end_h, end_m, 0)
        if punch_time < shift_end_time:
            # Belongs to previous day's shift
            prev_day = punch_in_ist.date() - timedelta(days=1)
            return prev_day.isoformat()

    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R2: Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 after shift_start.
    Then late_minutes = whole minutes (floor) elapsed since shift_start. Otherwise 0.
    """
    sh, sm = parse_time_str(shift_start)
    att_date = dt_date.fromisoformat(attendance_date_str)
    shift_start_ist = datetime(
        att_date.year, att_date.month, att_date.day, sh, sm, 0, tzinfo=IST
    )
    punch_in_ist = punch_in_utc.astimezone(IST)

    diff_sec = (punch_in_ist - shift_start_ist).total_seconds()
    if diff_sec > 600:  # strictly more than 10:00 (600 seconds)
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    hours = diff_sec / 3600.0
    return round_half_up(hours, 2)


def compute_overtime(
    punch_out_utc: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date_str: str,
) -> int:
    """
    R3: Overtime = whole minutes (floor) between shift_end and punch_out,
    counted only when >= 30; otherwise 0.
    For overnight shifts (shift_end <= shift_start), shift_end falls on the next calendar day.
    """
    eh, em = parse_time_str(shift_end)
    att_date = dt_date.fromisoformat(attendance_date_str)
    if shift_end <= shift_start:
        end_date = att_date + timedelta(days=1)
    else:
        end_date = att_date

    shift_end_ist = datetime(
        end_date.year, end_date.month, end_date.day, eh, em, 0, tzinfo=IST
    )
    punch_out_ist = punch_out_utc.astimezone(IST)

    diff_sec = (punch_out_ist - shift_end_ist).total_seconds()
    if diff_sec >= 1800:  # at least 30 minutes (30 * 60 = 1800s)
        return int(diff_sec // 60)
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: After punch-out, half_day is true when the rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days(start_date: dt_date, end_date: dt_date) -> int:
    """R7: Working days Mon-Fri (no holiday calendar)."""
    if start_date > end_date:
        return 0
    cur = start_date
    count = 0
    while cur <= end_date:
        if cur.weekday() < 5:  # Monday is 0, Friday is 4
            count += 1
        cur += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Pydantic Schemas matching OpenAPI 3.0.3 Contract exactly
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: Optional[int] = Field(None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Formatters: MongoDB Document -> API Response
# --------------------------------------------------------------------------- #
def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    created_at = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(created_at),
    }


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    history_entries = []
    for h in doc.get("history", []):
        formatted_changes = {}
        for k, v in h.get("changes", {}).items():
            from_val = v.get("from")
            to_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                formatted_changes[k] = {
                    "from": dt_to_epoch_ms(from_val) if isinstance(from_val, datetime) else from_val,
                    "to": dt_to_epoch_ms(to_val) if isinstance(to_val, datetime) else to_val,
                }
            else:
                formatted_changes[k] = {"from": from_val, "to": to_val}

        history_entries.append({
            "at": dt_to_epoch_ms(h["at"]),
            "by": h["by"],
            "reason": h["reason"],
            "changes": formatted_changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_entries,
    }


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe. Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}")


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """Create an employee. Enforces uniqueness via MongoDB unique index."""
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start must differ from shift_end")

    now = now_utc_truncated()
    doc = body.model_dump()
    doc["created_at"] = now

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [format_employee_doc(d) for d in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """
    Record a punch-in. Creates the day's record per R1 & R2.
    Race safety guaranteed by database unique index on (emp_code, date).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_utc = epoch_ms_to_utc_dt(body.punched_at)
    else:
        punch_in_utc = now_utc_truncated()

    attendance_date = get_attendance_date(
        punch_in_utc, emp["shift_start"], emp["shift_end"]
    )
    late_mins = compute_late_minutes(
        punch_in_utc, emp["shift_start"], attendance_date
    )

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out. Closes the employee's most recent record whose punch_in <= punched_at.
    Computes work_hours (R4), overtime_minutes (R3), and half_day (R5).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = epoch_ms_to_utc_dt(body.punched_at)
    else:
        punch_out_utc = now_utc_truncated()

    # Find most recent punch-in at or before punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="That record is already punched out")

    punch_in_utc = record["punch_in"]
    if punch_out_utc <= punch_in_utc:
        raise HTTPException(status_code=422, detail="punched_at must be strictly greater than punch_in")

    if (punch_out_utc - punch_in_utc) > timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    work_hrs = compute_work_hours(punch_in_utc, punch_out_utc)
    half_day = compute_half_day(work_hrs)
    overtime_mins = compute_overtime(
        punch_out_utc, emp["shift_start"], emp["shift_end"], record["date"]
    )

    # Atomic update to prevent race conditions on simultaneous punch-outs
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hrs,
                "overtime_minutes": overtime_mins,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="That record is already punched out")

    return format_attendance_doc(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records sorted by date desc, then emp_code asc."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be greater than date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """
    Regularize (manually correct) one day's record.
    Recomputes derived fields and appends exactly one entry to history.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    new_status = body.status if body.status is not None else record["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE",
            )
        final_punch_in = None
        final_punch_out = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            final_punch_in = epoch_ms_to_utc_dt(body.punch_in)
        else:
            final_punch_in = record.get("punch_in")

        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

        # Verify punch_in stays on record's attendance date per R1
        att_date = get_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
        if att_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in belongs to attendance date {att_date}, expected {date}",
            )

        if body.punch_out is not None:
            final_punch_out = epoch_ms_to_utc_dt(body.punch_out)
        else:
            final_punch_out = record.get("punch_out")

        if final_punch_out is not None:
            if final_punch_out <= final_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be strictly greater than punch_in")
            if (final_punch_out - final_punch_in) > timedelta(hours=24):
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

        final_late_minutes = compute_late_minutes(final_punch_in, emp["shift_start"], date)
        if final_punch_out is not None:
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_half_day = compute_half_day(final_work_hours)
            final_overtime_minutes = compute_overtime(
                final_punch_out, emp["shift_start"], emp["shift_end"], date
            )
        else:
            final_work_hours = None
            final_half_day = False
            final_overtime_minutes = 0

    # Determine changes
    candidate_values = {
        "status": new_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        "work_hours": final_work_hours,
        "late_minutes": final_late_minutes,
        "overtime_minutes": final_overtime_minutes,
        "half_day": final_half_day,
    }

    changes: Dict[str, Any] = {}
    for k, new_v in candidate_values.items():
        old_v = record.get(k)
        if old_v != new_v:
            changes[k] = {"from": old_v, "to": new_v}

    if not changes:
        raise HTTPException(status_code=422, detail="A request that changes nothing is rejected")

    history_entry = {
        "at": now_utc_truncated(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency: history size check prevents silent overwrite
    current_history_len = len(record.get("history", []))
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": {"$size": current_history_len}},
        {
            "$set": candidate_values,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return format_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregation Pipelines)
# --------------------------------------------------------------------------- #
def get_month_dates(month_str: str) -> tuple[dt_date, dt_date]:
    """Helper to get first and last calendar day of a YYYY-MM month."""
    parts = month_str.split("-")
    y, m = int(parts[0]), int(parts[1])
    first_day = dt_date(y, m, 1)
    if m == 12:
        last_day = dt_date(y, 12, 31)
    else:
        last_day = dt_date(y, m + 1, 1) - timedelta(days=1)
    return first_day, last_day


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    first_day, last_day = get_month_dates(month)
    joined_on = dt_date.fromisoformat(emp["joined_on"])
    start_working = max(first_day, joined_on)
    working_days = count_working_days(start_working, last_day)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()},
            }
        },
        {
            "$project": {
                "status": 1,
                "date": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "day_of_week": {
                    "$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": ["$day_of_week", 5]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100.0, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    """Department summary for a month across both collections."""
    first_day, last_day = get_month_dates(month)
    first_day_str = first_day.isoformat()
    last_day_str = last_day.isoformat()

    emp_match: Dict[str, Any] = {"joined_on": {"$lte": last_day_str}}
    if department is not None:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", first_day_str]},
                                    {"$lte": ["$date", last_day_str]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "work_hours": 1,
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "day_of_week": {
                                "$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                            },
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "flattened_logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$flattened_logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$lte": ["$$l.day_of_week", 5]},
                                        ]
                                    },
                                }
                            },
                            "as": "pl",
                            "in": {"$cond": ["$$pl.half_day", 0.5, 1.0]},
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$flattened_logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": "$flattened_logs.late_minutes"
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$flattened_logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$flattened_logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
                "work_hours_list": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$flattened_logs",
                                "as": "l",
                                "cond": {
                                    "$and": [
                                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$$l.work_hours", None]},
                                    ]
                                },
                            }
                        },
                        "as": "wl",
                        "in": "$$wl.work_hours",
                    }
                },
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]

    cursor = db.employees.aggregate(pipeline)
    items = []
    for doc in cursor:
        wh_list = doc.get("work_hours_list", [])
        if wh_list:
            avg_wh = round_half_up(sum(wh_list) / len(wh_list), 2)
        else:
            avg_wh = None

        items.append({
            "department": doc["department"],
            "headcount": doc["headcount"],
            "present_days": float(doc.get("present_days", 0.0)),
            "avg_work_hours": avg_wh,
            "late_count": doc.get("late_count", 0),
            "total_late_minutes": doc.get("total_late_minutes", 0),
            "leave_count": doc.get("leave_count", 0),
            "on_duty_count": doc.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    """Late-comers leaderboard for a month using standard competition ranking ($rank)."""
    first_day, last_day = get_month_dates(month)
    first_day_str = first_day.isoformat()
    last_day_str = last_day.isoformat()

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": first_day_str, "$lte": last_day_str},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Daily attendance trend for a department with gap-filling and moving average."""
    f_dt = dt_date.fromisoformat(from_date)
    t_dt = dt_date.fromisoformat(to_date)
    if t_dt < f_dt:
        raise HTTPException(status_code=422, detail="'to' date cannot be before 'from' date")

    day_count = (t_dt - f_dt).days + 1
    if day_count > 92:
        raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

    # Check department existence
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime(f_dt.year, f_dt.month, f_dt.day, tzinfo=timezone.utc),
                        datetime(t_dt.year, t_dt.month, t_dt.day, tzinfo=timezone.utc)
                        + timedelta(days=1),
                    ],
                },
            }
        },
        {
            "$project": {
                "date": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}
                },
                "date_dt": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$sort": {"date": ASCENDING}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$project": {
                "date": 1,
                "present_count": 1,
                "late_count": 1,
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]},
                "is_working_day": {
                    "$lte": [
                        {"$isoDayOfWeek": "$date_dt"},
                        5,
                    ]
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
    ]

    cursor = db.attendance_logs.aggregate(pipeline)
    items = []
    for row in cursor:
        att_rate = row.get("attendance_rate")
        mov_avg = row.get("moving_avg_7d")
        items.append({
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": float(row["present_count"]),
            "late_count": int(row["late_count"]),
            "attendance_rate": round_half_up(att_rate, 4) if att_rate is not None else None,
            "moving_avg_7d": round_half_up(mov_avg, 4) if mov_avg is not None else None,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Runs explain with executionStats verbosity for the main query or aggregation
    of the named endpoint, built exactly as that endpoint builds it.
    """
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must not be greater than date_to")

        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        skip = (page - 1) * page_size
        explain_res = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")

        first_day, last_day = get_month_dates(month)
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "total_late_minutes": {"$sum": "$late_minutes"},
                }
            },
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")

        first_day, last_day = get_month_dates(month)
        emp_match: Dict[str, Any] = {"joined_on": {"$lte": last_day.isoformat()}}
        if department is not None:
            emp_match["department"] = department

        pipeline = [
            {"$match": emp_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", first_day.isoformat()]},
                                        {"$lte": ["$date", last_day.isoformat()]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_res,
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")

        first_day, last_day = get_month_dates(month)
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                }
            },
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422, detail="department, from, and to are required"
            )

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": from_date, "$lte": to_date},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {"$group": {"_id": "$date", "count": {"$sum": 1}}},
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }
