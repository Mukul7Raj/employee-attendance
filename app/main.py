"""
Employee Attendance & Analytics API
HROne Engineering Technical Assignment

All-in-one minimal, clean implementation:
- Startup index creation (idempotent)
- Rules R1 - R10 (IST timezone, grace period, overtime, half-up rounding)
- Concurrency safety (MongoDB unique indexes, atomic updates)
- Analytics pipelines (aggregation, standard competition ranking, densify gap filling)
"""
import os
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

# =========================================================================== #
# 1. DATABASE & STARTUP CONFIGURATION
# =========================================================================== #
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MS = 100000000000
MAX_EPOCH_MS = 4102444800000


def ensure_indexes():
    """Create all required indexes idempotently at startup."""
    try:
        # Guarantee 1: Unique employee code
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        # Headcount filtering (R9)
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
        db.employees.create_index([("joined_on", ASCENDING)])

        # Guarantee 2: One log per employee per attendance day (R1, punch-in race safety)
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
        )
        # Attendance listing sort: date DESC, emp_code ASC (R10)
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        # Fast punch-out: most recent punch-in for employee
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
        # Fast late leaderboard filter: date range + late_minutes > 0
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# =========================================================================== #
# 2. CORE BUSINESS RULES (R1 - R5, R7, R8)
# =========================================================================== #
def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """R8: 2 decimals half-up (rates: 4 decimals half-up). Uses Decimal, not banker's rounding."""
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("10") ** (-decimals)
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts a UTC datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_utc_dt(ms: int) -> datetime:
    """R1: Converts epoch ms to UTC datetime truncated to whole seconds."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def now_utc_truncated() -> datetime:
    """Returns current UTC time truncated to whole seconds."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_hh_mm(time_str: str) -> tuple[int, int]:
    h, m = map(int, time_str.split(":"))
    return h, m


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date is the IST calendar date of punch_in.
    Overnight shift exception (shift_end <= shift_start):
    If punch-in IST time is strictly earlier than shift_end, it belongs to the previous day's shift.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    if shift_end <= shift_start:
        end_h, end_m = parse_hh_mm(shift_end)
        if punch_in_ist.time() < time(end_h, end_m, 0):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R2: 10 min grace period. Late only if strictly > 10:00 (600s) after shift_start.
    When late: floor minutes elapsed since shift_start. Otherwise 0.
    """
    sh, sm = parse_hh_mm(shift_start)
    att_d = dt_date.fromisoformat(attendance_date_str)
    shift_start_ist = datetime(att_d.year, att_d.month, att_d.day, sh, sm, 0, tzinfo=IST)
    diff_sec = (punch_in_utc.astimezone(IST) - shift_start_ist).total_seconds()
    return int(diff_sec // 60) if diff_sec > 600 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    return round_half_up(diff_sec / 3600.0, 2)


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, att_date_str: str) -> int:
    """
    R3: Overtime = floor minutes between shift_end and punch_out, counted only when >= 30; else 0.
    Overnight shifts: shift_end falls on next calendar day.
    """
    eh, em = parse_hh_mm(shift_end)
    att_d = dt_date.fromisoformat(att_date_str)
    end_date = att_d + timedelta(days=1) if shift_end <= shift_start else att_d
    shift_end_ist = datetime(end_date.year, end_date.month, end_date.day, eh, em, 0, tzinfo=IST)
    diff_sec = (punch_out_utc.astimezone(IST) - shift_end_ist).total_seconds()
    return int(diff_sec // 60) if diff_sec >= 1800 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: True when rounded work_hours < 4.50."""
    return work_hours is not None and work_hours < 4.50


def count_working_days(start_d: dt_date, end_d: dt_date) -> int:
    """R7: Mon-Fri working days between start_d and end_d inclusive."""
    if start_d > end_d:
        return 0
    cur, count = start_d, 0
    while cur <= end_d:
        if cur.weekday() < 5:  # Mon=0, Fri=4
            count += 1
        cur += timedelta(days=1)
    return count


def get_month_range(month_str: str) -> tuple[dt_date, dt_date]:
    """Returns first and last calendar dates of YYYY-MM."""
    y, m = map(int, month_str.split("-"))
    first_d = dt_date(y, m, 1)
    last_d = dt_date(y, 12, 31) if m == 12 else (dt_date(y, m + 1, 1) - timedelta(days=1))
    return first_d, last_d


# =========================================================================== #
# 3. PYDANTIC SCHEMAS
# =========================================================================== #
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


# =========================================================================== #
# 4. RESPONSE FORMATTERS
# =========================================================================== #
def format_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


def format_attendance(doc: Dict[str, Any]) -> Dict[str, Any]:
    history_list = []
    for h in doc.get("history", []):
        formatted_changes = {}
        for k, v in h.get("changes", {}).items():
            fr, to = v.get("from"), v.get("to")
            if k in ("punch_in", "punch_out"):
                formatted_changes[k] = {
                    "from": dt_to_epoch_ms(fr) if isinstance(fr, datetime) else fr,
                    "to": dt_to_epoch_ms(to) if isinstance(to, datetime) else to,
                }
            else:
                formatted_changes[k] = {"from": fr, "to": to}

        history_list.append({
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
        "history": history_list,
    }


# =========================================================================== #
# 5. CRUD ENDPOINTS
# =========================================================================== #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe. Returns 200 only if MongoDB pings; else 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """Creates employee. Unique emp_code enforced via MongoDB unique index."""
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start must differ from shift_end")

    doc = body.model_dump()
    doc["created_at"] = now_utc_truncated()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return format_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Lists employees sorted by emp_code ASC."""
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    return {
        "items": [format_employee(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """
    Records punch-in. Race-safety guaranteed by unique index on (emp_code, date).
    Returns 404 if employee missing, 409 on duplicate date.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    punch_in_utc = epoch_ms_to_utc_dt(body.punched_at) if body.punched_at else now_utc_truncated()
    att_date = get_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_utc, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
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
    return format_attendance(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Records punch-out on most recent punch-in.
    Atomic find_one_and_update prevents race conditions (returns 409 for loser).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    punch_out_utc = epoch_ms_to_utc_dt(body.punched_at) if body.punched_at else now_utc_truncated()
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
    overtime_mins = compute_overtime(punch_out_utc, emp["shift_start"], emp["shift_end"], record["date"])

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
    return format_attendance(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Paginated attendance list sorted by date DESC, emp_code ASC."""
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
    return {
        "items": [format_attendance(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """
    Manual correction: recomputes derived fields and appends exactly one audit history entry.
    Optimistic lock on history size prevents concurrent lost updates.
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
            raise HTTPException(status_code=422, detail="punch times not allowed for ABSENT/LEAVE")
        f_in, f_out, f_wh, f_late, f_ot, f_half = None, None, None, 0, 0, False
    else:
        f_in = epoch_ms_to_utc_dt(body.punch_in) if body.punch_in is not None else record.get("punch_in")
        if f_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")

        if get_attendance_date(f_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on attendance date")

        f_out = epoch_ms_to_utc_dt(body.punch_out) if body.punch_out is not None else record.get("punch_out")
        if f_out is not None:
            if f_out <= f_in:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if (f_out - f_in) > timedelta(hours=24):
                raise HTTPException(status_code=422, detail="punch_out cannot exceed 24 hours")

        f_late = compute_late_minutes(f_in, emp["shift_start"], date)
        if f_out is not None:
            f_wh = compute_work_hours(f_in, f_out)
            f_half = compute_half_day(f_wh)
            f_ot = compute_overtime(f_out, emp["shift_start"], emp["shift_end"], date)
        else:
            f_wh, f_half, f_ot = None, False, 0

    candidates = {
        "status": new_status,
        "punch_in": f_in,
        "punch_out": f_out,
        "work_hours": f_wh,
        "late_minutes": f_late,
        "overtime_minutes": f_ot,
        "half_day": f_half,
    }

    changes = {k: {"from": record.get(k), "to": v} for k, v in candidates.items() if record.get(k) != v}
    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    history_entry = {
        "at": now_utc_truncated(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Match history size for concurrency check
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": {"$size": len(record.get("history", []))}},
        {"$set": candidates, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")
    return format_attendance(updated)


# =========================================================================== #
# 6. ANALYTICS PIPELINES
# =========================================================================== #
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee (Aggregation)."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    first_d, last_d = get_month_range(month)
    working_days = count_working_days(max(first_d, dt_date.fromisoformat(emp["joined_on"])), last_d)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_d.isoformat(), "$lte": last_d.isoformat()}}},
        {
            "$group": {
                "_id": None,
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]},
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    row = res[0] if res else {}
    present_days = float(row.get("present_days", 0.0))
    att_pct = round_half_up((present_days / working_days) * 100.0, 2) if working_days > 0 else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    """Department summary across both collections. Counts employees with zero logs (R9)."""
    first_d, last_d = get_month_range(month)
    emp_match: Dict[str, Any] = {"joined_on": {"$lte": last_d.isoformat()}}
    if department:
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
                                    {"$gte": ["$date", first_d.isoformat()]},
                                    {"$lte": ["$date", last_d.isoformat()]},
                                ]
                            }
                        }
                    }
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
        {"$sort": {"_id": ASCENDING}},
    ]

    items = []
    for doc in db.employees.aggregate(pipeline):
        flattened = [item for sublist in doc.get("all_logs", []) for item in sublist]
        present_days = 0.0
        late_count, total_late = 0, 0
        leave_count, on_duty_count = 0, 0
        wh_list = []

        for log in flattened:
            st = log.get("status")
            half = log.get("half_day", False)
            lm = log.get("late_minutes", 0)
            wh = log.get("work_hours")

            # Check Mon-Fri
            log_d = dt_date.fromisoformat(log["date"])
            if st in ("PRESENT", "WFH", "ON_DUTY") and log_d.weekday() < 5:
                present_days += 0.5 if half else 1.0

            if st in ("PRESENT", "WFH", "ON_DUTY") and wh is not None:
                wh_list.append(wh)

            if lm > 0:
                late_count += 1
                total_late += lm
            if st == "LEAVE":
                leave_count += 1
            if st == "ON_DUTY":
                on_duty_count += 1

        avg_wh = round_half_up(sum(wh_list) / len(wh_list), 2) if wh_list else None
        items.append({
            "department": doc["_id"],
            "headcount": doc["headcount"],
            "present_days": present_days,
            "avg_work_hours": avg_wh,
            "late_count": late_count,
            "total_late_minutes": total_late,
            "leave_count": leave_count,
            "on_duty_count": on_duty_count,
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    """Ranks employees by total late minutes using standard competition ranking ($rank)."""
    first_d, last_d = get_month_range(month)
    pipeline: List[Dict[str, Any]] = [
        {"$match": {"date": {"$gte": first_d.isoformat(), "$lte": last_d.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department:
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
        {"$setWindowFields": {"sortBy": {"total_late_minutes": DESCENDING}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ])
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Daily attendance trend with gap filling ($densify) and 7-day moving average."""
    f_d, t_d = dt_date.fromisoformat(from_date), dt_date.fromisoformat(to_date)
    if t_d < f_d:
        raise HTTPException(status_code=422, detail="'to' date cannot be before 'from' date")
    if (t_d - f_d).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range exceeds 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
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
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
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
                        datetime(f_d.year, f_d.month, f_d.day, tzinfo=timezone.utc),
                        datetime(t_d.year, t_d.month, t_d.day, tzinfo=timezone.utc) + timedelta(days=1),
                    ],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
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
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$date_dt"}, 5]},
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
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
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

    items = []
    for r in db.attendance_logs.aggregate(pipeline):
        ar = r.get("attendance_rate")
        ma = r.get("moving_avg_7d")
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": round_half_up(ar, 4) if ar is not None else None,
            "moving_avg_7d": round_half_up(ma, 4) if ma is not None else None,
        })
    return {"department": department, "items": items}


# =========================================================================== #
# 7. ADMIN EXPLAIN ENDPOINT
# =========================================================================== #
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
    """Runs MongoDB explain with executionStats on the named endpoint's primary query."""
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
        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        f_d, l_d = get_month_range(month)
        pipe = [{"$match": {"emp_code": emp_code, "date": {"$gte": f_d.isoformat(), "$lte": l_d.isoformat()}}}]
        res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        _, l_d = get_month_range(month)
        m: Dict[str, Any] = {"joined_on": {"$lte": l_d.isoformat()}}
        if department:
            m["department"] = department
        pipe = [{"$match": m}, {"$group": {"_id": "$department", "headcount": {"$sum": 1}}}]
        res = db.command("explain", {"aggregate": "employees", "pipeline": pipe, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": res}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        f_d, l_d = get_month_range(month)
        pipe = [
            {"$match": {"date": {"$gte": f_d.isoformat(), "$lte": l_d.isoformat()}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        ]
        res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        pipe = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
        ]
        res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}
