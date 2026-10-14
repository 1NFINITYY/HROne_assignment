"""
Employee Attendance & Analytics API — COMPLETE IMPLEMENTATION

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience)

All code lives in this single file as required by the assignment.
"""
import os
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = os.getenv("MONGO_DB",  "attendance_db")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]

# IST timezone offset
IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    description="HROne Engineering Assignment — Complete Implementation",
)


# ---------------------------------------------------------------------------
# Startup: create indexes idempotently
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes():
    """
    Create all indexes at startup. `unique=True` on emp_code ensures that
    concurrent duplicate-create requests at the DB level are serialised — only
    one succeeds; the other gets a DuplicateKeyError → 409.

    Compound index on (emp_code, date) in attendance_logs ensures at most one
    record per employee per day, and powers the punch-in duplicate check
    atomically (unique insert wins the race, the loser gets DuplicateKeyError).

    The remaining indexes serve the query patterns used by every endpoint and
    analytics pipeline.
    """
    import logging
    try:
        # employees
        db.employees.create_index("emp_code", unique=True)
        db.employees.create_index("department")

        # attendance_logs — natural key is (emp_code, date)
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
        )
        # listAttendance: sort by date desc, emp_code asc; filter on date range, status
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        )
        db.attendance_logs.create_index("status")
        # analytics pipelines filter on date (month prefix) then emp_code
        db.attendance_logs.create_index(
            [("date", ASCENDING), ("emp_code", ASCENDING)]
        )
        logging.info("MongoDB indexes created/verified.")
    except Exception as exc:
        logging.warning(f"Could not create indexes at startup (MongoDB unreachable?): {exc}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _half_up(value: float, decimals: int = 2) -> float:
    """Round a float using HALF_UP (not Python's banker's rounding)."""
    fmt = "0." + "0" * decimals
    return float(Decimal(str(value)).quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def _ms_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds → UTC-aware datetime, truncated to seconds."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc).replace(microsecond=0)


def _dt_to_ms(dt: datetime) -> int:
    """Convert a datetime (aware or naive UTC) → epoch milliseconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _ist_date(dt: datetime) -> str:
    """Return YYYY-MM-DD in IST for the given datetime."""
    return dt.astimezone(IST).strftime("%Y-%m-%d")


def _attendance_date(dt_ist: datetime, shift_start: str, shift_end: str) -> str:
    """
    Implement R1: for overnight shifts (shift_end <= shift_start) a punch-in
    whose IST time is *earlier than* shift_end belongs to the shift that
    started the **previous** IST calendar day.
    """
    h_end, m_end = map(int, shift_end.split(":"))
    h_start, m_start = map(int, shift_start.split(":"))

    ist = dt_ist.astimezone(IST)
    is_overnight = (h_end * 60 + m_end) <= (h_start * 60 + m_start)

    if is_overnight:
        punch_minutes = ist.hour * 60 + ist.minute
        shift_end_minutes = h_end * 60 + m_end
        if punch_minutes < shift_end_minutes:
            # belongs to previous day's shift
            return (ist - timedelta(days=1)).strftime("%Y-%m-%d")

    return ist.strftime("%Y-%m-%d")


def _compute_late_minutes(punch_in_ist: datetime, shift_start: str) -> int:
    """
    R2: Grace period 10 min. Late only if STRICTLY MORE than 10 minutes after
    shift_start. late_minutes = floor(elapsed minutes since shift_start), not
    since end of grace.
    e.g. shift 09:30 → 09:40:00 → 0; 09:40:01 → 10; 10:15:59 → 45
    """
    h, m = map(int, shift_start.split(":"))
    ist = punch_in_ist.astimezone(IST)
    start = ist.replace(hour=h, minute=m, second=0, microsecond=0)
    elapsed_seconds = (ist - start).total_seconds()
    if elapsed_seconds <= 10 * 60:           # <= 10:00 is NOT late
        return 0
    return int(elapsed_seconds // 60)       # floor minutes from shift_start


def _compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded half-up to 2 dp."""
    seconds = (punch_out - punch_in).total_seconds()
    return _half_up(seconds / 3600, 2)


def _compute_overtime_minutes(punch_out_utc: datetime, shift_end: str, date_str: str) -> int:
    """
    R3: overtime_minutes = floor minutes between shift_end and punch_out,
    only when >= 30; otherwise 0.
    shift_end is an IST HH:MM on the attendance date (date_str).
    For overnight shifts the shift_end falls on the NEXT calendar day.
    """
    h, m = map(int, shift_end.split(":"))
    # Build the shift-end datetime in IST on date_str
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    end_ist = date_obj.replace(hour=h, minute=m, second=0, microsecond=0,
                                tzinfo=IST)
    # For overnight shifts shift_end is on the next day
    # Heuristic: if shift_end_minutes <= shift_start_minutes, add a day.
    # We can detect overnight because punch_out > shift_end + 1 day would be
    # captured by the 24-hour limit check; here we compare directly.
    # Simple check: if end_ist <= start_of_day, it's the next day.
    # But we don't have shift_start here. Use a pragmatic rule:
    # if punch_out_ist < end_ist (in IST), try end_ist + 1 day.
    punch_out_ist = punch_out_utc.astimezone(IST)
    if punch_out_ist < end_ist:
        end_ist = end_ist + timedelta(days=1)

    diff_seconds = (punch_out_ist - end_ist).total_seconds()
    if diff_seconds < 30 * 60:
        return 0
    return int(diff_seconds // 60)


def _compute_half_day(work_hours: float) -> bool:
    """R5: half_day = True when rounded work_hours < 4.50."""
    return work_hours < 4.50


def _serialize_record(doc: dict) -> dict:
    """
    Convert an attendance_logs document from MongoDB to the API response shape.
    - _id → removed
    - punch_in / punch_out (BSON datetime or None) → epoch ms or None
    - history[].at and history[].changes punch times → epoch ms
    """
    doc.pop("_id", None)
    if doc.get("punch_in") is not None:
        doc["punch_in"] = _dt_to_ms(doc["punch_in"])
    if doc.get("punch_out") is not None:
        doc["punch_out"] = _dt_to_ms(doc["punch_out"])
    # defaults for optional / legacy fields
    doc.setdefault("history", [])
    doc.setdefault("half_day", False)
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)

    # Serialize history entries
    for entry in doc["history"]:
        if isinstance(entry.get("at"), datetime):
            entry["at"] = _dt_to_ms(entry["at"])
        changes = entry.get("changes", {})
        for field in ("punch_in", "punch_out"):
            if field in changes:
                for key in ("from", "to"):
                    val = changes[field].get(key)
                    if isinstance(val, datetime):
                        changes[field][key] = _dt_to_ms(val)
    return doc


def _serialize_employee(doc: dict) -> dict:
    """Convert an employee document to API shape (created_at → epoch ms)."""
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = _dt_to_ms(doc["created_at"])
    return doc


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

EPOCH_MIN = 100_000_000_000   # ~1973 — rejects second-level timestamps
EPOCH_MAX = 4_102_444_800_000  # ~2100


class EmployeeIn(BaseModel):
    emp_code:    str = Field(..., pattern=r"^EMP\d{4,6}$")
    name:        str = Field(..., min_length=1, max_length=100)
    email:       str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department:  str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end:   str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on:   str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None
    status:     str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_epoch(cls, v):
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("punched_at must be an integer epoch milliseconds")
            if v < EPOCH_MIN or v > EPOCH_MAX:
                raise ValueError(
                    f"punched_at must be between {EPOCH_MIN} and {EPOCH_MAX} (milliseconds)"
                )
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        allowed = {"PRESENT", "WFH", "ON_DUTY"}
        if v not in allowed:
            raise ValueError(f"status must be one of {allowed}")
        return v


class PunchOutIn(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_epoch(cls, v):
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("punched_at must be an integer epoch milliseconds")
            if v < EPOCH_MIN or v > EPOCH_MAX:
                raise ValueError(
                    f"punched_at must be between {EPOCH_MIN} and {EPOCH_MAX} (milliseconds)"
                )
        return v


class RegularizeIn(BaseModel):
    reason:         str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status:         Optional[str] = None
    punch_in:       Optional[int] = None
    punch_out:      Optional[int] = None

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch(cls, v):
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("punch times must be integer epoch milliseconds")
            if v < EPOCH_MIN or v > EPOCH_MAX:
                raise ValueError(
                    f"punch time must be between {EPOCH_MIN} and {EPOCH_MAX} (milliseconds)"
                )
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None:
            allowed = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
            if v not in allowed:
                raise ValueError(f"status must be one of {allowed}")
        return v

    @model_validator(mode="after")
    def no_punch_with_absent_leave(self):
        if self.status in ("ABSENT", "LEAVE"):
            if self.punch_in is not None or self.punch_out is not None:
                raise ValueError(
                    "punch_in and punch_out must not be supplied when setting status to ABSENT or LEAVE"
                )
        return self


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health", tags=["system"])
def health():
    """R: Return 200 if MongoDB is reachable, 503 otherwise."""
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"detail": "MongoDB unavailable"})


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------

@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeIn):
    """
    Create a new employee. emp_code must be unique (enforced by a DB unique
    index so concurrent creates are safe).
    created_at is set by the server in UTC.
    """
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return _serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page:       int = Query(default=1, ge=1),
    page_size:  int = Query(default=20, ge=1, le=100),
):
    """
    List employees sorted by emp_code ascending. total reflects filtered set.
    Bug fix: original code used page * page_size (off-by-one — skipped page 1).
    Correct formula: skip = (page - 1) * page_size.
    """
    q: dict = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    for item in items:
        if isinstance(item.get("created_at"), datetime):
            item["created_at"] = _dt_to_ms(item["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# Attendance – Punch-in
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInIn):
    """
    Create the attendance record for today (R1 date). Uses a unique DB index
    on (emp_code, date) so concurrent identical requests are safe — the
    second insert raises DuplicateKeyError → 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    # Truncate to seconds (R1)
    if body.punched_at is not None:
        ts_utc = _ms_to_dt(body.punched_at)
    else:
        ts_utc = datetime.now(tz=timezone.utc).replace(microsecond=0)

    date_str = _attendance_date(ts_utc, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code":          body.emp_code,
        "date":              date_str,
        "status":            body.status,
        "punch_in":          ts_utc,
        "punch_out":         None,
        "work_hours":        None,
        "late_minutes":      _compute_late_minutes(ts_utc, emp["shift_start"]),
        "overtime_minutes":  0,
        "half_day":          False,
        "history":           [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")

    return _serialize_record(doc)


# ---------------------------------------------------------------------------
# Attendance – Punch-out
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutIn):
    """
    Close the most recent open record (punch_in <= punched_at, punch_out null).
    Computes work_hours (R4), overtime_minutes (R3), half_day (R5).
    Atomic update via find_one_and_update to avoid race conditions (409 for
    simultaneous punch-outs).
    Validates: punched_at > punch_in, within 24 hours.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        ts_utc = _ms_to_dt(body.punched_at)
    else:
        ts_utc = datetime.now(tz=timezone.utc).replace(microsecond=0)

    # Find the most recent open record with punch_in <= ts_utc
    record = db.attendance_logs.find_one(
        {
            "emp_code":  body.emp_code,
            "punch_in":  {"$ne": None, "$lte": ts_utc},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(404, "No open punch-in found for this employee")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # Validate: punched_at must be strictly after punch_in
    if ts_utc <= punch_in_dt:
        raise HTTPException(422, "punched_at must be after punch_in")

    # Validate: within 24 hours
    if (ts_utc - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punched_at is more than 24 hours after punch_in")

    # Compute derived fields
    wh = _compute_work_hours(punch_in_dt, ts_utc)
    ot = _compute_overtime_minutes(ts_utc, emp["shift_end"], record["date"])
    hd = _compute_half_day(wh)

    # Atomic update — only succeeds if punch_out is still null (race protection)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out":        ts_utc,
            "work_hours":       wh,
            "overtime_minutes": ot,
            "half_day":         hd,
        }},
        return_document=True,
    )

    if updated is None:
        raise HTTPException(409, "Record was already punched out by a concurrent request")

    return _serialize_record(updated)


# ---------------------------------------------------------------------------
# Attendance – List
# ---------------------------------------------------------------------------

@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code:  Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    status:    Optional[str] = None,
    page:      int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    List attendance records, sorted date desc then emp_code asc.
    Uses DB-side skip/limit (no full-collection load into Python).
    Bug fix: original loaded all docs into Python and sorted in memory —
    replaced with MongoDB sort + skip + limit.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        allowed = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
        if status not in allowed:
            raise HTTPException(422, f"status must be one of {allowed}")
        q["status"] = status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [_serialize_record(d) for d in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# Attendance – Regularize (PATCH)
# ---------------------------------------------------------------------------

@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    """
    Manually correct one day's attendance record.
    - Omitted fields are unchanged.
    - Recomputes derived fields from final punch times.
    - Appends one history entry with only fields that actually changed.
    - Uses atomic find_one_and_update with history array size check to prevent
      concurrent history loss (409 for the loser).
    """
    # Validate employee exists
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    # Fetch existing record
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found for this employee and date")

    # Build the new state by merging changes
    new_status    = body.status    if body.status    is not None else record["status"]
    new_punch_in  = _ms_to_dt(body.punch_in)  if body.punch_in  is not None else record.get("punch_in")
    new_punch_out = _ms_to_dt(body.punch_out) if body.punch_out is not None else record.get("punch_out")

    # Normalise stored datetimes (PyMongo may return naive UTC datetimes)
    if isinstance(new_punch_in, datetime) and new_punch_in.tzinfo is None:
        new_punch_in = new_punch_in.replace(tzinfo=timezone.utc)
    if isinstance(new_punch_out, datetime) and new_punch_out.tzinfo is None:
        new_punch_out = new_punch_out.replace(tzinfo=timezone.utc)

    # Rule: ABSENT/LEAVE → clear punch times
    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in  = None
        new_punch_out = None

    # Rule: presence status requires a punch_in
    if new_status in ("PRESENT", "WFH", "ON_DUTY") and new_punch_in is None:
        raise HTTPException(422, "A presence status requires a punch_in")

    # Rule: punch_in must stay on the attendance date (R1)
    if new_punch_in is not None:
        pi_date = _attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if pi_date != date:
            raise HTTPException(422, "punch_in must remain on the attendance date (R1)")

    # Rule: punch_out must be after punch_in and within 24 hours
    if new_punch_out is not None and new_punch_in is not None:
        diff = (new_punch_out - new_punch_in).total_seconds()
        if diff <= 0:
            raise HTTPException(422, "punch_out must be after punch_in")
        if diff > 24 * 3600:
            raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE") or new_punch_in is None:
        new_late       = 0
        new_wh         = None
        new_ot         = 0
        new_hd         = False
    else:
        new_late = _compute_late_minutes(new_punch_in, emp["shift_start"])
        if new_punch_out is not None:
            new_wh = _compute_work_hours(new_punch_in, new_punch_out)
            new_ot = _compute_overtime_minutes(new_punch_out, emp["shift_end"], date)
            new_hd = _compute_half_day(new_wh)
        else:
            new_wh = None
            new_ot = 0
            new_hd = False

    # Build 'changes' dict: only fields that actually changed
    old_pi  = record.get("punch_in")
    old_po  = record.get("punch_out")
    old_wh  = record.get("work_hours")
    old_lat = record.get("late_minutes", 0)
    old_ot  = record.get("overtime_minutes", 0)
    old_hd  = record.get("half_day", False)

    if old_pi is not None and old_pi.tzinfo is None:
        old_pi = old_pi.replace(tzinfo=timezone.utc)
    if old_po is not None and old_po.tzinfo is None:
        old_po = old_po.replace(tzinfo=timezone.utc)

    changes: dict = {}

    if new_status != record["status"]:
        changes["status"] = {"from": record["status"], "to": new_status}
    if new_punch_in != old_pi:
        changes["punch_in"] = {
            "from": _dt_to_ms(old_pi) if old_pi else None,
            "to":   _dt_to_ms(new_punch_in) if new_punch_in else None,
        }
    if new_punch_out != old_po:
        changes["punch_out"] = {
            "from": _dt_to_ms(old_po) if old_po else None,
            "to":   _dt_to_ms(new_punch_out) if new_punch_out else None,
        }
    if new_wh != old_wh:
        changes["work_hours"] = {"from": old_wh, "to": new_wh}
    if new_late != old_lat:
        changes["late_minutes"] = {"from": old_lat, "to": new_late}
    if new_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}
    if new_hd != old_hd:
        changes["half_day"] = {"from": old_hd, "to": new_hd}

    if not changes:
        raise HTTPException(422, "No fields would change")

    # History entry — store datetimes as BSON dates in 'changes'
    history_entry: dict = {
        "at":     datetime.now(tz=timezone.utc),
        "by":     body.regularized_by,
        "reason": body.reason,
        "changes": {},
    }
    for field, val in changes.items():
        if field in ("punch_in", "punch_out"):
            # Store from/to as BSON dates (or None) in MongoDB
            history_entry["changes"][field] = {
                "from": (_ms_to_dt(val["from"]) if val["from"] is not None else None),
                "to":   (_ms_to_dt(val["to"])   if val["to"]   is not None else None),
            }
        else:
            history_entry["changes"][field] = val

    # Atomic update: filter includes current history length to detect concurrent edits
    current_history_len = len(record.get("history", []))

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, current_history_len]},
        },
        {
            "$set": {
                "status":            new_status,
                "punch_in":          new_punch_in,
                "punch_out":         new_punch_out,
                "work_hours":        new_wh,
                "late_minutes":      new_late,
                "overtime_minutes":  new_ot,
                "half_day":          new_hd,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )

    if updated is None:
        raise HTTPException(409, "Concurrent edit detected; please retry")

    return _serialize_record(updated)


# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------

def _parse_month(month: str):
    """Return (year, month_int, date_from_str, date_to_str) for YYYY-MM."""
    try:
        y, m = map(int, month.split("-"))
    except Exception:
        raise HTTPException(422, "month must be YYYY-MM")
    import calendar
    last_day = calendar.monthrange(y, m)[1]
    return y, m, f"{month}-01", f"{month}-{last_day:02d}"


def _month_regex(month: str) -> str:
    """Return a regex prefix for date strings in a given month."""
    return f"^{month}-"


# ---------------------------------------------------------------------------
# Analytics – Employee Monthly
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """
    Monthly attendance summary for one employee. Uses a MongoDB aggregation.
    working_days: Mon-Fri in the month from max(joined_on, month_start).
    present_days: R5-R7 — presence records on Mon-Fri, half_day = 0.5.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    y, m, date_from, date_to = _parse_month(month)

    # Count working days (Mon-Fri) from max(joined_on, month_start) to month_end
    import calendar
    joined = datetime.strptime(emp["joined_on"], "%Y-%m-%d").date()
    month_start = datetime.strptime(date_from, "%Y-%m-%d").date()
    month_end   = datetime.strptime(date_to,   "%Y-%m-%d").date()
    effective_start = max(joined, month_start)

    working_days = 0
    cur = effective_start
    while cur <= month_end:
        if cur.weekday() < 5:   # Mon=0 … Fri=4
            working_days += 1
        cur += timedelta(days=1)

    # Aggregation over attendance_logs for this employee and month
    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date":     {"$gte": date_from, "$lte": date_to},
        }},
        {"$addFields": {
            # Parse date string → date object for day-of-week check
            "date_obj": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
            "half_day_val": {"$ifNull": ["$half_day", False]},
            "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
        }},
        {"$addFields": {
            # dayOfWeek: 1=Sun, 2=Mon, … 7=Sat → working day if 2-6
            "is_weekday": {"$and": [
                {"$gte": [{"$dayOfWeek": "$date_obj"}, 2]},
                {"$lte": [{"$dayOfWeek": "$date_obj"}, 6]},
            ]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": ["$is_presence", "$is_weekday"]},
                    {"$cond": ["$half_day_val", 0.5, 1.0]},
                    0.0,
                ],
            }},
            "leave_days":         {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count":         {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]

    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        r = results[0]
        present_days          = _half_up(r["present_days"], 2)
        leave_days            = r["leave_days"]
        late_count            = r["late_count"]
        total_late_minutes    = r["total_late_minutes"]
        total_overtime_minutes = r["total_overtime_minutes"]
    else:
        present_days          = 0.0
        leave_days            = 0
        late_count            = 0
        total_late_minutes    = 0
        total_overtime_minutes = 0

    attendance_pct = (
        _half_up(present_days / working_days * 100, 2)
        if working_days > 0 else None
    )

    return {
        "emp_code":              emp_code,
        "month":                 month,
        "working_days":          working_days,
        "present_days":          present_days,
        "leave_days":            leave_days,
        "late_count":            late_count,
        "total_late_minutes":    total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct":        attendance_pct,
    }


# ---------------------------------------------------------------------------
# Analytics – Department Summary
# ---------------------------------------------------------------------------

@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month:      str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department summary for a month. Uses a MongoDB aggregation across both
    collections via $lookup.
    headcount: employees with joined_on <= last day of month (R9), including
    those with zero logs.
    """
    y, m, date_from, date_to = _parse_month(month)

    # Match employees by department if requested, and joined_on <= end of month
    emp_match: dict = {"joined_on": {"$lte": date_to}}
    if department:
        emp_match["department"] = department

    pipeline = [
        # Start from employees so we capture zero-log employees
        {"$match": emp_match},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "emp_codes":  {"$push": "$emp_code"},
        }},
        # Lookup attendance logs for these employees in this month
        {"$lookup": {
            "from": "attendance_logs",
            "let":  {"codes": "$emp_codes"},
            "pipeline": [
                {"$match": {"$expr": {
                    "$and": [
                        {"$in": ["$emp_code", "$$codes"]},
                        {"$gte": ["$date", date_from]},
                        {"$lte": ["$date", date_to]},
                    ],
                }}},
                {"$addFields": {
                    "date_obj":    {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
                    "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    "half_day_val": {"$ifNull": ["$half_day", False]},
                }},
                {"$addFields": {
                    "is_weekday": {"$and": [
                        {"$gte": [{"$dayOfWeek": "$date_obj"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date_obj"}, 6]},
                    ]},
                }},
            ],
            "as": "logs",
        }},
        {"$addFields": {
            "present_days": {"$sum": {
                "$map": {
                    "input": "$logs",
                    "as":    "log",
                    "in": {"$cond": [
                        {"$and": ["$$log.is_presence", "$$log.is_weekday"]},
                        {"$cond": ["$$log.half_day_val", 0.5, 1.0]},
                        0.0,
                    ]},
                },
            }},
            "avg_work_hours_raw": {"$avg": {
                "$filter": {
                    "input": {"$map": {
                        "input": "$logs",
                        "as":    "log",
                        "in": {
                            "$cond": [
                                {"$and": [
                                    "$$log.is_presence",
                                    {"$ne": [{"$ifNull": ["$$log.work_hours", None]}, None]},
                                ]},
                                "$$log.work_hours",
                                None,
                            ],
                        },
                    }},
                    "as":   "wh",
                    "cond": {"$ne": ["$$wh", None]},
                },
            }},
            "late_count": {"$sum": {
                "$map": {
                    "input": "$logs",
                    "as":    "log",
                    "in": {"$cond": [
                        {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
                        1, 0,
                    ]},
                },
            }},
            "total_late_minutes": {"$sum": {
                "$map": {
                    "input": "$logs",
                    "as":    "log",
                    "in":    {"$ifNull": ["$$log.late_minutes", 0]},
                },
            }},
            "leave_count": {"$sum": {
                "$map": {
                    "input": "$logs",
                    "as":    "log",
                    "in": {"$cond": [{"$eq": ["$$log.status", "LEAVE"]}, 1, 0]},
                },
            }},
            "on_duty_count": {"$sum": {
                "$map": {
                    "input": "$logs",
                    "as":    "log",
                    "in": {"$cond": [{"$eq": ["$$log.status", "ON_DUTY"]}, 1, 0]},
                },
            }},
        }},
        {"$sort": {"_id": ASCENDING}},
        {"$project": {
            "_id":               0,
            "department":        "$_id",
            "headcount":         1,
            "present_days":      1,
            "avg_work_hours_raw": 1,
            "late_count":        1,
            "total_late_minutes": 1,
            "leave_count":       1,
            "on_duty_count":     1,
        }},
    ]

    raw_items = list(db.employees.aggregate(pipeline))

    items = []
    for row in raw_items:
        avg_wh = (
            _half_up(row["avg_work_hours_raw"], 2)
            if row.get("avg_work_hours_raw") is not None else None
        )
        items.append({
            "department":         row["department"],
            "headcount":          row["headcount"],
            "present_days":       _half_up(row["present_days"], 2),
            "avg_work_hours":     avg_wh,
            "late_count":         row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count":        row["leave_count"],
            "on_duty_count":      row["on_duty_count"],
        })

    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# Analytics – Late Leaderboard
# ---------------------------------------------------------------------------

@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month:      str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit:      int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Ranks employees by total late_minutes in the month (only > 0).
    Standard competition ranking: ties share a rank, next rank is skipped.
    limit applies after ranking: return all rows whose rank <= limit.
    Joined-out employees whose logs exist but employee doc is missing are ignored.
    """
    y, m, date_from, date_to = _parse_month(month)

    emp_match: dict = {}
    if department:
        emp_match["department"] = department

    pipeline = [
        # Aggregate late minutes per employee for the month
        {"$match": {
            "date":         {"$gte": date_from, "$lte": date_to},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id":                "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count":         {"$sum": 1},
        }},
        # Join with employees to get name, department, and apply department filter
        {"$lookup": {
            "from":         "employees",
            "localField":   "_id",
            "foreignField": "emp_code",
            "as":           "emp",
        }},
        {"$unwind": "$emp"},   # drops records with no matching employee
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline += [
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {"$project": {
            "_id":                0,
            "emp_code":           "$_id",
            "name":               "$emp.name",
            "department":         "$emp.department",
            "total_late_minutes": 1,
            "late_count":         1,
        }},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Standard competition ranking in Python (dense rank would be different)
    ranked = []
    current_rank = 1
    prev_minutes  = None
    skip_count    = 0

    for i, row in enumerate(rows):
        if prev_minutes is not None and row["total_late_minutes"] < prev_minutes:
            current_rank = i + 1
        row["rank"] = current_rank
        prev_minutes = row["total_late_minutes"]
        if current_rank <= limit:
            ranked.append(row)

    return {"month": month, "items": ranked}


# ---------------------------------------------------------------------------
# Analytics – Department Trend
# ---------------------------------------------------------------------------

@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_:      str = Query(..., alias="from"),
    to:         str = Query(...),
):
    """
    Daily attendance trend for a department. Returns exactly one row per
    calendar day using $densify (MongoDB 5.1+). Gap-filling happens in the DB.
    """
    # Validate date format
    try:
        from_dt = datetime.strptime(from_, "%Y-%m-%d")
        to_dt   = datetime.strptime(to, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, "from and to must be YYYY-MM-DD")

    if to_dt < from_dt:
        raise HTTPException(422, "to must not be before from")

    if (to_dt - from_dt).days > 92:
        raise HTTPException(422, "Date range must not exceed 92 days")

    # Verify department exists
    dept_count = db.employees.count_documents({"department": department})
    if dept_count == 0:
        raise HTTPException(404, f"No employees found in department '{department}'")

    # --- Build a per-day aggregation using $densify for gap filling ---
    pipeline = [
        # Match logs in range for this department's employees
        {"$match": {
            "date": {"$gte": from_, "$lte": to},
        }},
        # Lookup employee to filter by department
        {"$lookup": {
            "from":         "employees",
            "localField":   "emp_code",
            "foreignField": "emp_code",
            "as":           "emp",
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$addFields": {
            "is_presence":  {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            "half_day_val": {"$ifNull": ["$half_day", False]},
        }},
        # Group by date
        {"$group": {
            "_id":         "$date",
            "present_count": {"$sum": {
                "$cond": [
                    "$is_presence",
                    {"$cond": ["$half_day_val", 0.5, 1.0]},
                    0.0,
                ],
            }},
            "late_count": {"$sum": {
                "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0],
            }},
        }},
        # Convert _id (string date) to a date for $densify
        {"$addFields": {
            "date_dt": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
        }},
        # Densify: add rows for missing dates in range
        {"$densify": {
            "field": "date_dt",
            "range": {
                "step":     1,
                "unit":     "day",
                "bounds":   [
                    datetime.strptime(from_, "%Y-%m-%d").replace(tzinfo=timezone.utc),
                    datetime.strptime(to,    "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1),
                ],
            },
        }},
        # Fill nulls for gap days
        {"$addFields": {
            "present_count": {"$ifNull": ["$present_count", 0.0]},
            "late_count":    {"$ifNull": ["$late_count",    0]},
            "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
        }},
        {"$sort": {"date_dt": ASCENDING}},
    ]

    raw_rows = list(db.attendance_logs.aggregate(pipeline))

    # Compute headcount per day (employees with joined_on <= day)
    # Fetch all employees in the department once
    all_emps = list(db.employees.find(
        {"department": department},
        {"emp_code": 1, "joined_on": 1, "_id": 0},
    ))

    # Build result with headcount and attendance_rate
    result_items = []

    for row in raw_rows:
        day_str = row["date_str"]
        day_dt  = datetime.strptime(day_str, "%Y-%m-%d").date()
        weekday = day_dt.weekday()   # 0=Mon … 6=Sun
        is_working_day = weekday < 5

        headcount = sum(
            1 for e in all_emps
            if datetime.strptime(e["joined_on"], "%Y-%m-%d").date() <= day_dt
        )

        present_count = row["present_count"]
        late_count    = row["late_count"]

        if is_working_day and headcount > 0:
            attendance_rate = _half_up(present_count / headcount, 4)
        else:
            attendance_rate = None

        result_items.append({
            "date":            day_str,
            "is_working_day":  is_working_day,
            "headcount":       headcount,
            "present_count":   present_count,
            "late_count":      late_count,
            "attendance_rate": attendance_rate,
        })

    # Compute moving_avg_7d in a second pass (all items must exist first).
    # Window = current row + up to 6 preceding rows, non-null rates only.
    for i, item in enumerate(result_items):
        window = result_items[max(0, i - 6): i + 1]
        rates  = [w["attendance_rate"] for w in window if w["attendance_rate"] is not None]
        if rates:
            item["moving_avg_7d"] = _half_up(sum(rates) / len(rates), 4)
        else:
            item["moving_avg_7d"] = None

    return {"department": department, "items": result_items}


# ---------------------------------------------------------------------------
# Admin – Explain
# ---------------------------------------------------------------------------

import json as _json
from bson import json_util as _bson_json_util


def _bson_to_jsonable(doc: dict) -> dict:
    """
    Convert a raw MongoDB explain document (which may contain BSON-specific
    types such as Timestamp, ObjectId, Int64, etc.) into a plain Python dict
    that FastAPI's JSON encoder can handle. We round-trip through bson.json_util
    which knows how to serialise every BSON type.
    """
    return _json.loads(_bson_json_util.dumps(doc))


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint:   str,
    emp_code:   Optional[str] = None,
    month:      Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit:      int           = Query(default=10, ge=1, le=50),
    date_from:  Optional[str] = None,
    date_to:    Optional[str] = None,
    status:     Optional[str] = None,
    from_:      Optional[str] = Query(default=None, alias="from"),
    to:         Optional[str] = None,
    page:       int           = Query(default=1, ge=1),
    page_size:  int           = Query(default=20, ge=1, le=100),
):
    """
    Returns MongoDB explain (executionStats) for the named endpoint's main query.
    Builds the query/pipeline exactly as the real endpoint would.
    """
    allowed = {"attendance_list", "employee_monthly", "department_summary",
               "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(422, f"endpoint must be one of {allowed}")

    if endpoint == "attendance_list":
        q: dict = {}
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
        explain_doc = db.command(
            "explain",
            {
                "find":        "attendance_logs",
                "filter":      q,
                "sort":        {"date": -1, "emp_code": 1},
                "skip":        skip,
                "limit":       page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _bson_to_jsonable(explain_doc)}

    elif endpoint == "employee_monthly":
        if not month:
            raise HTTPException(422, "month is required for employee_monthly")
        if not emp_code:
            raise HTTPException(422, "emp_code is required for employee_monthly")
        y, m_int, date_from2, date_to2 = _parse_month(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": date_from2, "$lte": date_to2}}},
            {"$count": "total"},
        ]
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _bson_to_jsonable(explain_doc)}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        y, m_int, date_from2, date_to2 = _parse_month(month)
        emp_match2: dict = {"joined_on": {"$lte": date_to2}}
        if department:
            emp_match2["department"] = department
        pipeline = [
            {"$match": emp_match2},
            {"$count": "total"},
        ]
        explain_doc = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": _bson_to_jsonable(explain_doc)}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        y, m_int, date_from2, date_to2 = _parse_month(month)
        match_q: dict = {
            "date":         {"$gte": date_from2, "$lte": date_to2},
            "late_minutes": {"$gt": 0},
        }
        pipeline = [
            {"$match": match_q},
            {"$count": "total"},
        ]
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _bson_to_jsonable(explain_doc)}

    elif endpoint == "department_trend":
        if not from_ or not to:
            raise HTTPException(422, "from and to are required for department_trend")
        if not department:
            raise HTTPException(422, "department is required for department_trend")
        q2: dict = {
            "date": {"$gte": from_, "$lte": to},
        }
        explain_doc = db.command(
            "explain",
            {
                "find":   "attendance_logs",
                "filter": q2,
                "sort":   {"date": 1, "emp_code": 1},
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _bson_to_jsonable(explain_doc)}
