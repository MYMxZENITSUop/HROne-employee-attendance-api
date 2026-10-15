"""Employee Attendance & Analytics API - STARTER"""

import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone, date, time
from typing import Literal, Optional, Annotated

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument, ASCENDING, DESCENDING
from decimal import Decimal, ROUND_HALF_UP
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]


def ensure_indexes():
    # Employee codes must be unique.
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uniq_employee_code",
    )

    # One attendance record per employee per attendance date.
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uniq_employee_attendance_date",
    )

    # Support department and analytics aggregation lookups.
    db.employees.create_index(
        [("department", 1), ("joined_on", 1)],
        name="department_joined_on",
    )
    db.employees.create_index(
        [("joined_on", 1), ("department", 1)],
        name="joined_on_department",
    )
    db.attendance_logs.create_index(
        [("status", 1), ("date", -1), ("emp_code", 1)],
        name="attendance_status_date_employee",
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("punch_in", -1)],
        name="attendance_employee_punch_in",
    )
    db.attendance_logs.create_index(
        [("date", 1), ("late_minutes", 1)],
        name="attendance_date_late_minutes",
    )

    # Support attendance filtering and date-based queries.
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="attendance_date_employee",
    )


IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def require_db():
    """Return the configured database or respond with 503 if MongoDB is unavailable."""
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    return db
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


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
# Helpers
# --------------------------------------------------------------------------- #


def epoch_ms_to_ist(value: int) -> datetime:
    """Validate epoch milliseconds, truncate to seconds, and return IST."""
    if not 100_000_000_000 <= value <= 4_102_444_800_000:
        raise HTTPException(422, "Invalid punched_at timestamp")

    try:
        instant = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise HTTPException(422, "Invalid punched_at timestamp")

    instant = instant.replace(microsecond=0)
    return instant.astimezone(IST)


def compute_late_minutes(
    punch_in: datetime, shift_start: str, shift_end: Optional[str] = None
) -> int:
    """Calculate lateness against the correct IST shift start, including overnight shifts."""
    punch_in_ist = punch_in.astimezone(IST)
    start_time = datetime.strptime(shift_start, "%H:%M").time()
    start_date = punch_in_ist.date()

    if shift_end is not None:
        end_time = datetime.strptime(shift_end, "%H:%M").time()
        if end_time <= start_time and punch_in_ist.time() < end_time:
            start_date -= timedelta(days=1)

    start = datetime.combine(start_date, start_time, tzinfo=IST)
    elapsed_seconds = (punch_in_ist - start).total_seconds()
    return int(elapsed_seconds // 60) if elapsed_seconds > 10 * 60 else 0



def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    hours = (seconds / Decimal("3600")).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return float(hours)


def compute_overtime(
    punch_out: datetime, shift_end: str, date_str: str, shift_start: str
) -> int:
    """Return whole overtime minutes, only when overtime reaches 30 minutes."""
    attendance_date = date.fromisoformat(date_str)
    start_time = datetime.strptime(shift_start, "%H:%M").time()
    end_time = datetime.strptime(shift_end, "%H:%M").time()
    end_date = attendance_date + (timedelta(days=1) if end_time <= start_time else timedelta())
    shift_end_ist = datetime.combine(end_date, end_time, tzinfo=IST)
    punch_out_utc = utc_datetime(punch_out)
    overtime = int((punch_out_utc - shift_end_ist.astimezone(UTC)).total_seconds() // 60)
    return overtime if overtime >= 30 else 0



def utc_datetime(value):
    """Treat PyMongo's naive datetimes as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def epoch_ms(value):
    value = utc_datetime(value)
    return int(value.timestamp() * 1000) if value else None


def calculate_derived(emp, punch_in, punch_out):
    if punch_in is None:
        return {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }

    punch_in_ist = utc_datetime(punch_in).astimezone(IST)
    late_minutes = compute_late_minutes(
        punch_in_ist, emp["shift_start"], emp["shift_end"]
    )

    if punch_out is None:
        return {
            "late_minutes": late_minutes,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }

    punch_out_utc = utc_datetime(punch_out)
    punch_in_utc = utc_datetime(punch_in)

    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)

    shift_start = datetime.strptime(
        emp["shift_start"], "%H:%M"
    ).time()
    shift_end = datetime.strptime(
        emp["shift_end"], "%H:%M"
    ).time()

    shift_date = punch_in_ist.date()
    shift_end_date = shift_date

    if shift_end <= shift_start:
        shift_end_date += timedelta(days=1)

    shift_end_ist = datetime.combine(
        shift_end_date, shift_end, tzinfo=IST
    )

    overtime = int(
        (
            punch_out_utc - shift_end_ist.astimezone(timezone.utc)
        ).total_seconds() // 60
    )
    overtime = max(0, overtime)
    if overtime < 30:
        overtime = 0

    return {
        "late_minutes": late_minutes,
        "work_hours": work_hours,
        "overtime_minutes": overtime,
        "half_day": work_hours < 4.50,
    }



# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value: str) -> str:
        if not re.fullmatch(r"EMP\d{4,6}", value):
            raise ValueError("emp_code must be EMP followed by 4-6 digits")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 100:
            raise ValueError("name must contain 1-100 characters")
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 120 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("email must be a valid email address of at most 120 characters")
        return value

    @field_validator("department")
    @classmethod
    def validate_department(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 50:
            raise ValueError("department must contain 1-50 characters")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("shift times must use 24-hour HH:MM format")
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date") from exc
        if parsed.isoformat() != value:
            raise ValueError("joined_on must use YYYY-MM-DD format")
        return value

    @model_validator(mode="after")
    def validate_shift_boundaries(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal[
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    ]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str
    regularized_by: str



def validate_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise HTTPException(422, "month must use YYYY-MM")
    start = date.fromisoformat(value + "-01")
    next_month = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1)
    return start, next_month


def mongo_round_half_up(expression, places: int):
    scale = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expression, scale]}, 0.5]}}, scale]}


def employee_monthly_pipeline(emp_code: str, month_start: date, month_next: date) -> list:
    start_text, next_text = month_start.isoformat(), month_next.isoformat()
    start_expr = {"$dateFromString": {"dateString": start_text}}
    next_expr = {"$dateFromString": {"dateString": next_text}}
    joined_expr = {"$dateFromString": {"dateString": "$joined_on"}}
    joined_start = {"$max": [joined_expr, start_expr]}
    days_since_join = {
        "$cond": [
            {"$lt": ["$joined_on", next_text]},
            {"$dateDiff": {"startDate": joined_start, "endDate": next_expr, "unit": "day"}},
            0,
        ]
    }
    weekdays = {"$size": {"$filter": {
        "input": {"$map": {
            "input": {"$range": [0, days_since_join]},
            "as": "offset",
            "in": {"$dayOfWeek": {"$dateAdd": {"startDate": joined_start, "unit": "day", "amount": "$$offset"}}},
        }},
        "as": "dow",
        "cond": {"$and": [{"$gte": ["$$dow", 2]}, {"$lte": ["$$dow", 6]}]},
    }}}
    present_values = {"$map": {
        "input": {"$filter": {
            "input": "$logs",
            "as": "log",
            "cond": {"$and": [
                {"$in": ["$$log.status", list(PRESENCE_STATUSES)]},
                {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$log.date"}}}, [2, 3, 4, 5, 6]]},
            ]},
        }},
        "as": "log",
        "in": {"$cond": [{"$ifNull": ["$$log.half_day", False]}, 0.5, 1]},
    }}
    return [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": start_text, "$lt": next_text}}}],
            "as": "logs",
        }},
        {"$set": {
            "working_days": weekdays,
            "present_days": {"$sum": present_values},
            "leave_days": {"$size": {"$filter": {"input": "$logs", "as": "log", "cond": {"$eq": ["$$log.status", "LEAVE"]}}}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "log", "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}}}},
            "total_late_minutes": {"$sum": {"$map": {"input": "$logs", "as": "log", "in": {"$ifNull": ["$$log.late_minutes", 0]}}}},
            "total_overtime_minutes": {"$sum": {"$map": {"input": "$logs", "as": "log", "in": {"$ifNull": ["$$log.overtime_minutes", 0]}}}},
        }},
        {"$set": {"attendance_pct": {"$cond": [
            {"$gt": ["$working_days", 0]},
            mongo_round_half_up({"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 100]}, 2),
            None,
        ]}}},
        {"$project": {
            "_id": 0, "emp_code": 1, "month": {"$literal": month_start.strftime("%Y-%m")},
            "working_days": 1, "present_days": 1, "leave_days": 1, "late_count": 1,
            "total_late_minutes": 1, "total_overtime_minutes": 1, "attendance_pct": 1,
        }},
    ]


def department_summary_pipeline(month_start: date, month_next: date, department: Optional[str]) -> list:
    start_text, next_text = month_start.isoformat(), month_next.isoformat()
    employee_match = {"joined_on": {"$lt": next_text}}
    if department is not None:
        employee_match["department"] = department
    weekday_presence = {"$and": [
        {"$in": ["$$log.status", list(PRESENCE_STATUSES)]},
        {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$log.date"}}}, [2, 3, 4, 5, 6]]},
    ]}
    return [
        {"$match": employee_match},
        {"$group": {
            "_id": "$department", "department": {"$first": "$department"},
            "headcount": {"$sum": 1}, "emp_codes": {"$push": "$emp_code"},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_codes",
            "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": start_text, "$lt": next_text}}}],
            "as": "logs",
        }},
        {"$set": {
            "present_days": {"$sum": {"$map": {
                "input": {"$filter": {"input": "$logs", "as": "log", "cond": weekday_presence}},
                "as": "log",
                "in": {"$cond": [{"$ifNull": ["$$log.half_day", False]}, 0.5, 1]},
            }}},
            "avg_work_hours": {"$let": {
                "vars": {"eligible": {"$filter": {
                    "input": "$logs", "as": "log",
                    "cond": {"$and": [
                        {"$in": ["$$log.status", list(PRESENCE_STATUSES)]},
                        {"$isNumber": "$$log.work_hours"},
                    ]},
                }}},
                "in": {"$cond": [
                    {"$gt": [{"$size": "$$eligible"}, 0]},
                    mongo_round_half_up({"$avg": {"$map": {"input": "$$eligible", "as": "log", "in": "$$log.work_hours"}}}, 2),
                    None,
                ]},
            }},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "log", "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}}}},
            "total_late_minutes": {"$sum": {"$map": {"input": "$logs", "as": "log", "in": {"$ifNull": ["$$log.late_minutes", 0]}}}},
            "leave_count": {"$size": {"$filter": {"input": "$logs", "as": "log", "cond": {"$eq": ["$$log.status", "LEAVE"]}}}},
            "on_duty_count": {"$size": {"$filter": {"input": "$logs", "as": "log", "cond": {"$eq": ["$$log.status", "ON_DUTY"]}}}},
        }},
        {"$project": {"_id": 0, "emp_codes": 0, "logs": 0}},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(month_start: date, month_next: date, limit: int, department: Optional[str]) -> list:
    start_text, next_text = month_start.isoformat(), month_next.isoformat()
    pipeline = [
        {"$match": {"date": {"$gte": start_text, "$lt": next_text}, "late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$group": {
            "_id": "$emp_code", "emp_code": {"$first": "$emp_code"},
            "name": {"$first": "$employee.name"}, "department": {"$first": "$employee.department"},
            "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1},
        }},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1,
            "total_late_minutes": 1, "late_count": 1,
        }},
    ])
    return pipeline


def department_trend_pipeline(department: str, start_day: date, end_day: date) -> list:
    start_dt = datetime.combine(start_day, time.min, tzinfo=UTC)
    end_exclusive = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=UTC)
    day_text_expr = {"$dateToString": {"format": "%Y-%m-%d", "date": "$day", "timezone": "UTC"}}
    return [
        {"$match": {"department": department}},
        {"$group": {
            "_id": "$department",
            "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}},
            "day": {"$min": {"$literal": start_dt}},
        }},
        {"$densify": {
            "field": "day",
            "partitionByFields": ["employees"],
            "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_exclusive]},
        }},
        {"$set": {
            "day_text": day_text_expr,
            "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, [2, 3, 4, 5, 6]]},
            "headcount": {"$size": {"$filter": {
                "input": "$employees", "as": "employee",
                "cond": {"$lte": ["$$employee.joined_on", day_text_expr]},
            }}},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "day_text",
            "foreignField": "date",
            "let": {"employee_codes": "$employees"},
            "pipeline": [{"$match": {"$expr": {"$in": [
                "$emp_code",
                {"$map": {"input": "$$employee_codes", "as": "employee", "in": "$$employee.emp_code"}},
            ]}}}],
            "as": "logs",
        }},
        {"$set": {
            "present_count": {"$sum": {"$map": {
                "input": {"$filter": {"input": "$logs", "as": "log", "cond": {"$in": ["$$log.status", list(PRESENCE_STATUSES)]}}},
                "as": "log",
                "in": {"$cond": [{"$ifNull": ["$$log.half_day", False]}, 0.5, 1]},
            }}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "log", "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}}}},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            mongo_round_half_up({"$divide": ["$present_count", "$headcount"]}, 4),
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$project": {
            "_id": 0, "date": "$day_text", "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1,
            "attendance_rate": {"$cond": [
                {"$eq": ["$attendance_rate", None]}, None, mongo_round_half_up("$attendance_rate", 4),
            ]},
            "moving_avg_7d": {"$cond": [
                {"$eq": ["$moving_avg_7d", None]}, None, mongo_round_half_up("$moving_avg_7d", 4),
            ]},
        }},
        {"$sort": {"date": 1}},
    ]


def run_explain(collection, pipeline_or_query, operation: str, sort=None, skip=0, limit=None):
    if operation == "find":
        find_command = {
            "find": collection.name,
            "filter": pipeline_or_query,
            "sort": dict(sort or []),
            "skip": skip,
        }
        if limit is not None:
            find_command["limit"] = limit
        return collection.database.command(
            "explain", find_command, verbosity="executionStats"
        )
    command = {
        "aggregate": collection.name, "pipeline": pipeline_or_query,
        "cursor": {}, "allowDiskUse": True,
    }
    return db.command("explain", command, verbosity="executionStats")


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="MongoDB unavailable",
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        # The unique index also protects against simultaneous requests.
        raise HTTPException(409, "emp_code already exists")

    doc.pop("_id", None)

    # API timestamps must be returned as epoch milliseconds.
    doc["created_at"] = int(doc["created_at"].timestamp() * 1000)

    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)

    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one(
        {"emp_code": body.emp_code},
        {"_id": 0},
    )

    if emp is None:
        raise HTTPException(404, "Employee not found")

    if emp["shift_start"] == emp["shift_end"]:
        raise HTTPException(422, "Shift start and end must differ")

    if body.punched_at is None:
        ts = datetime.now(IST).replace(microsecond=0)
    else:
        ts = epoch_ms_to_ist(body.punched_at)

    shift_start = datetime.strptime(
        emp["shift_start"], "%H:%M"
    ).time()
    shift_end = datetime.strptime(
        emp["shift_end"], "%H:%M"
    ).time()

    # For an overnight shift, early-morning punches belong
    # to the shift that started on the previous calendar day.
    attendance_date = ts.date()

    if shift_end <= shift_start and ts.time() < shift_end:
        attendance_date -= timedelta(days=1)

    date_str = attendance_date.isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            409,
            "Already punched in for this date",
        )

    # The API exposes epoch milliseconds, not BSON datetimes
    # or MongoDB's internal _id.
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": int(ts.timestamp() * 1000),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": doc["late_minutes"],
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }



@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is None:
        ts = datetime.now(IST).replace(microsecond=0)
    else:
        ts = epoch_ms_to_ist(body.punched_at)

    # BSON datetimes are stored as UTC instants.
    ts_utc = ts.astimezone(timezone.utc).replace(tzinfo=None)

    # Find the latest punch-in that happened at or before punch-out.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts_utc},
        },
        sort=[("punch_in", -1)],
    )

    if record is None:
        # A punch-in exists, but it happened after the requested punch-out.
        future_record = db.attendance_logs.find_one({
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$gt": ts_utc},
        })

        if future_record is not None:
            raise HTTPException(422, "Invalid punch-out time")

        raise HTTPException(404, "No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    elapsed = (
        ts_utc.replace(tzinfo=timezone.utc) - punch_in_utc
    ).total_seconds()

    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        raise HTTPException(422, "Invalid punch-out time")

    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    # The attendance date is the date the shift started.
    attendance_date = datetime.strptime(
        record["date"], "%Y-%m-%d"
    ).date()


    hours = compute_work_hours(
        punch_in_utc.replace(tzinfo=timezone.utc),
        ts_utc.replace(tzinfo=timezone.utc),
    )

    overtime = compute_overtime(ts_utc.replace(tzinfo=timezone.utc), shift_end, record["date"], shift_start)

    half_day = hours < 4.50

    # Atomic update: only one concurrent punch-out can succeed.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$or": [
                {"punch_out": None},
                {"punch_out": {"$exists": False}},
            ],
        },
        {
            "$set": {
                "punch_out": ts_utc,
                "work_hours": hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(409, "Already punched out")

    updated.pop("_id", None)
    updated["punch_in"] = int(
        record["punch_in"].replace(tzinfo=timezone.utc).timestamp() * 1000
    )
    updated["punch_out"] = int(
        ts_utc.replace(tzinfo=timezone.utc).timestamp() * 1000
    )
    updated.setdefault("late_minutes", 0)
    updated.setdefault("overtime_minutes", 0)
    updated.setdefault("half_day", False)
    updated.setdefault("history", [])

    return updated


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    # Validate the calendar date.
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
        if parsed_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "Invalid attendance date")

    if not 5 <= len(body.reason) <= 200:
        raise HTTPException(422, "reason must contain 5-200 characters")

    if not 1 <= len(body.regularized_by) <= 50:
        raise HTTPException(422, "regularized_by must contain 1-50 characters")

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date,
    })
    if record is None:
        raise HTTPException(404, "Attendance record not found")

    # Only process fields actually supplied by the client.
    supplied = body.model_fields_set
    status = body.status if "status" in supplied else record["status"]

    old_in = record.get("punch_in")
    old_out = record.get("punch_out")

    punch_in = old_in
    punch_out = old_out

    if "punch_in" in supplied:
        punch_in = epoch_ms_to_ist(body.punch_in).astimezone(timezone.utc).replace(tzinfo=None) if body.punch_in is not None else None

    if "punch_out" in supplied:
        punch_out = epoch_ms_to_ist(body.punch_out).astimezone(timezone.utc).replace(tzinfo=None) if body.punch_out is not None else None

    presence_statuses = {"PRESENT", "WFH", "ON_DUTY"}

    if status in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(
                422,
                "ABSENT and LEAVE cannot include punch times",
            )
        punch_in = None
        punch_out = None

    elif punch_in is None:
        raise HTTPException(
            422,
            "Presence status requires punch_in",
        )

    if punch_in is not None:
        punch_in_ist = utc_datetime(punch_in).astimezone(IST)
        expected_date = punch_in_ist.date()

        start = datetime.strptime(
            emp["shift_start"], "%H:%M"
        ).time()
        end = datetime.strptime(
            emp["shift_end"], "%H:%M"
        ).time()

        if end <= start and punch_in_ist.time() < end:
            expected_date -= timedelta(days=1)

        if expected_date.isoformat() != date:
            raise HTTPException(
                422,
                "punch_in must belong to the attendance date",
            )

    if punch_out is not None:
        if punch_in is None:
            raise HTTPException(422, "punch_out requires punch_in")

        elapsed = (
            utc_datetime(punch_out) - utc_datetime(punch_in)
        ).total_seconds()

        if elapsed <= 0 or elapsed > 24 * 60 * 60:
            raise HTTPException(
                422,
                "punch_out must be after punch_in and within 24 hours",
            )

    derived = calculate_derived(emp, punch_in, punch_out)

    old_values = {
        "status": record["status"],
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }

    new_values = {
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        **derived,
    }

    changes = {}
    for key, new_value in new_values.items():
        old_value = old_values[key]

        if key in {"punch_in", "punch_out"}:
            old_value = utc_datetime(old_value)
            new_value = utc_datetime(new_value)

        if old_value != new_value:
            changes[key] = {
                "from": old_value,
                "to": new_value,
            }

    if not changes:
        raise HTTPException(422, "Request makes no changes")

    history_entry = {
        "at": datetime.now(timezone.utc).replace(tzinfo=None),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency: another correction that changes history
    # first causes this update to match nothing instead of losing history.
    query = {
        "_id": record["_id"],
        "history": record.get("history", []),
    }

    updated = db.attendance_logs.find_one_and_update(
        query,
        {
            "$set": new_values,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(409, "Attendance record changed; retry")

    # Convert API instants, including nested history changes, to milliseconds.
    updated.pop("_id", None)
    updated["punch_in"] = epoch_ms(updated.get("punch_in"))
    updated["punch_out"] = epoch_ms(updated.get("punch_out"))

    for entry in updated.get("history", []):
        entry["at"] = epoch_ms(entry["at"])
        for change in entry.get("changes", {}).values():
            if change.get("from") is not None and isinstance(
                change["from"], datetime
            ):
                change["from"] = epoch_ms(change["from"])
            if change.get("to") is not None and isinstance(
                change["to"], datetime
            ):
                change["to"] = epoch_ms(change["to"])
    return updated


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    # Validate date filters before querying MongoDB.
    for value, field in ((date_from, "date_from"), (date_to, "date_to")):
        if value is not None:
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d").date()
                if parsed.isoformat() != value:
                    raise ValueError
            except ValueError:
                raise HTTPException(422, f"{field} must use YYYY-MM-DD")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    page_docs = list(
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    # The API contract uses epoch milliseconds, never BSON datetime values.
    for record in page_docs:
        record["punch_in"] = epoch_ms(record.get("punch_in"))
        record["punch_out"] = epoch_ms(record.get("punch_out"))
        for entry in record.get("history", []):
            entry["at"] = epoch_ms(entry.get("at"))
            for change in entry.get("changes", {}).values():
                for key in ("from", "to"):
                    value = change.get(key)
                    if isinstance(value, datetime):
                        change[key] = epoch_ms(value)

    return {
        "items": page_docs,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Analytics and query-plan endpoints
# --------------------------------------------------------------------------- #


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
):
    database = require_db()
    if database.employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
        raise HTTPException(404, "employee not found")
    month_start, month_next = validate_month(month)
    results = list(database.employees.aggregate(employee_monthly_pipeline(emp_code, month_start, month_next), allowDiskUse=True))
    if not results:
        raise HTTPException(404, "employee not found")
    return results[0]


@app.get("/analytics/departments/summary")
def department_summary(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    department: Optional[str] = None,
):
    database = require_db()
    month_start, month_next = validate_month(month)
    rows = list(database.employees.aggregate(department_summary_pipeline(month_start, month_next, department), allowDiskUse=True))
    return {"month": month, "items": rows}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    database = require_db()
    month_start, month_next = validate_month(month)
    rows = list(database.attendance_logs.aggregate(leaderboard_pipeline(month_start, month_next, limit, department), allowDiskUse=True))
    return {"month": month, "items": rows}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: Annotated[date, Query(alias="from")],
    to_date: Annotated[date, Query(alias="to")],
):
    database = require_db()
    if to_date < from_date:
        raise HTTPException(422, "to must not be before from")
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(422, "date range must not exceed 92 days")
    if database.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    rows = list(database.employees.aggregate(department_trend_pipeline(department, from_date, to_date), allowDiskUse=True))
    return {"department": department, "items": rows}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    from_date: Optional[date] = Query(default=None, alias="from"),
    to_date: Optional[date] = Query(default=None, alias="to"),
):
    database = require_db()
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not be after date_to")
        query = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if date_from is not None or date_to is not None:
            query["date"] = {}
            if date_from is not None:
                query["date"]["$gte"] = date_from.isoformat()
            if date_to is not None:
                query["date"]["$lte"] = date_to.isoformat()
        if status is not None:
            query["status"] = status
        explanation = run_explain(
            database.attendance_logs, query, "find",
            sort=[("date", DESCENDING), ("emp_code", ASCENDING)],
            skip=(page - 1) * page_size, limit=page_size,
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explanation}

    if endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(422, "emp_code and month are required")
        month_start, month_next = validate_month(month)
        pipeline = employee_monthly_pipeline(emp_code, month_start, month_next)
        collection, collection_name = database.employees, "employees"
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(422, "month is required")
        month_start, month_next = validate_month(month)
        pipeline = department_summary_pipeline(month_start, month_next, department)
        collection, collection_name = database.employees, "employees"
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(422, "month is required")
        month_start, month_next = validate_month(month)
        pipeline = leaderboard_pipeline(month_start, month_next, limit, department)
        collection, collection_name = database.attendance_logs, "attendance_logs"
    else:
        if department is None or from_date is None or to_date is None:
            raise HTTPException(422, "department, from, and to are required")
        if to_date < from_date:
            raise HTTPException(422, "to must not be before from")
        if (to_date - from_date).days + 1 > 92:
            raise HTTPException(422, "date range must not exceed 92 days")
        pipeline = department_trend_pipeline(department, from_date, to_date)
        collection, collection_name = database.employees, "employees"

    explanation = run_explain(collection, pipeline, "aggregate")
    return {"endpoint": endpoint, "collection": collection_name, "explain": explanation}
