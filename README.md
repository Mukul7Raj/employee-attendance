# Employee Attendance & Analytics API

A high-performance Employee Attendance & Analytics API built with **Python 3.11+**, **FastAPI**, and **MongoDB** implementing the HROne Engineering Technical Assignment.

---

## Overview

The API implements an end-to-end attendance management system complying with the OpenAPI 3.0.3 specification and business rules (R1 – R10):
- **Punch-In & Punch-Out**: Race-safe operations enforcing IST time zone rules, overnight shifts, 10-minute grace period late calculation, work hour derivation, half-day detection, and overtime tracking.
- **Manual Corrections (Regularization)**: Audit trail preserving immutability via embedded `history` arrays tracking field diffs, author, reason, and timestamp.
- **Database-Level Analytics**: High-performance aggregation pipelines for:
  - Monthly employee attendance statistics.
  - Department summary accounting for headcount with zero attendance logs.
  - Late-comers leaderboard implementing standard competition ranking (`$rank`) with post-ranking limit filtering.
  - Department daily trend with calendar gap filling (`$densify`) and 7-day moving averages (`$setWindowFields`).
- **Explain Endpoint**: Production introspection running MongoDB `explain` with `executionStats` to verify index coverage (`IXSCAN`) and zero collection scans (`COLLSCAN`).

---

## Tech Stack

- **Python**: 3.11+
- **Framework**: FastAPI 0.115.0
- **Server**: Uvicorn 0.30.0
- **Database Driver**: PyMongo 4.8.0
- **Validation**: Pydantic 2.9.0
- **Environment**: python-dotenv 1.0.1

---

## Submission Structure

```text
├── app/
│   └── main.py              # Complete FastAPI application (helpers, schemas, endpoints, indexes)
├── requirements.txt         # Core dependencies
├── REVIEW.md                # Detailed audit of starter defects and fixes
├── DECISIONS.md             # Key architectural decisions, concurrency & index rationale
├── README.md                # Setup, running, and API documentation
├── openapi.yaml             # API contract (OpenAPI 3.0.3)
├── DATA_MODEL.md            # Data model and Extended JSON schema documentation
├── sample_seed.py           # Sample data loader
└── sample_data/
    ├── employees.json       # Seed documents for employees
    └── attendance_logs.json # Seed documents for attendance logs
```

---

## Local Setup & Running

### 1. Create Virtual Environment & Install Dependencies
```bash
python -m venv .venv
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure Environment
Set `MONGO_URI` and `MONGO_DB` via environment variables or a local `.env` file:
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```
*(MongoDB 6.0+ or MongoDB Atlas free tier M0 works)*

### 3. Seed Sample Data (Optional)
```bash
python sample_seed.py
```

### 4. Run the API
```bash
uvicorn app.main:app --port 8000 --reload
```
Interactive API documentation is accessible at `http://localhost:8000/docs`.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Readiness probe (pings MongoDB; returns 200 or 503) |
| `POST` | `/employees` | Create employee (unique `emp_code`, validates shifts) |
| `GET` | `/employees` | List employees (paginated, sorted by `emp_code ASC`) |
| `POST` | `/attendance/punch-in` | Record punch-in (race-safe, late detection, overnight shift handling) |
| `POST` | `/attendance/punch-out` | Record punch-out (work hours, overtime, half day) |
| `GET` | `/attendance` | List attendance records (paginated, sorted by `date DESC, emp_code ASC`) |
| `PATCH` | `/attendance/{emp_code}/{date}` | Manual regularization with audit trail history |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly employee attendance summary |
| `GET` | `/analytics/departments/summary` | Department summary across both collections |
| `GET` | `/analytics/leaderboard/late` | Late leaderboard with standard competition ranking |
| `GET` | `/analytics/departments/{department}/trend` | Department daily trend with `$densify` gap filling |
| `GET` | `/admin/explain/{endpoint}` | Execution plan output checking index scans |

---

## Key Design Principles

1. **Epoch Milliseconds at Boundary**: All instants are exchanged as 64-bit epoch milliseconds and persisted as UTC BSON datetimes. Dates (`YYYY-MM-DD`) and times (`HH:MM`) remain strings to prevent timezone ambiguities.
2. **Database-Level Concurrency**: Zero check-then-insert antipatterns. Unique indexes on `employees.emp_code` and `attendance_logs.(emp_code, date)` guarantee strict race safety. Atomic `find_one_and_update` guards punch-outs and optimistic history updates.
3. **Index-Covered Queries**: All query and aggregation pipelines utilize composite indexes, eliminating in-memory sorting and full collection scans.
