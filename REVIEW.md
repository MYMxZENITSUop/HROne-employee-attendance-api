# REVIEW.md

## Starter Code Review

### 1. `compute_late_minutes` — Overnight shifts

- **What was wrong:** The original helper constructed the shift start on the punch-in's calendar date, which could calculate lateness incorrectly for early-morning punches on overnight shifts.
- **How to notice:** Test a 22:00–06:00 shift with a punch at 02:00.
- **How it was fixed:** The final helper accepts the shift end time and handles overnight shifts by adjusting the shift-start date when necessary. The punch-in endpoint also derives the appropriate attendance date for overnight shifts.

### 2. `compute_overtime` — Overnight shifts and threshold

- **What was wrong:** The original helper calculated shift end on the supplied date and did not enforce the 30-minute overtime threshold.
- **How to notice:** Test a 22:00–06:00 shift with a punch-out at 06:20 the next day.
- **How it was fixed:** The final helper handles overnight shift-end calculations and applies the 30-minute threshold. The punch-out endpoint also performs its own overnight-aware overtime calculation.

### 3. `compute_work_hours` — Rounding

- **What was wrong:** Python's built-in `round()` uses ties-to-even rounding, which does not guarantee the required half-up rounding behavior.
- **How to notice:** Test durations close to a two-decimal-hour rounding boundary.
- **How it was fixed:** The final code uses `Decimal` with `ROUND_HALF_UP`.

### 4. `/health` — Database readiness

- **What was wrong:** The starter returned `{"status": "ok"}` without checking MongoDB connectivity.
- **How to notice:** Stop MongoDB and call `GET /health`.
- **How it was fixed:** The final endpoint pings MongoDB and returns HTTP 503 when the database is unavailable.

### 5. `create_employee` — Duplicate employee codes

- **What was wrong:** Checking for an existing employee before inserting does not prevent simultaneous requests from inserting duplicates.
- **How to notice:** Send concurrent requests with the same `emp_code`.
- **How it was fixed:** The final code creates a unique index on `emp_code` and converts `DuplicateKeyError` to HTTP 409.

### 6. `create_employee` — Timestamp format

- **What was wrong:** The starter used `datetime.now()`, creating a naive local timestamp, and returned it without converting it to epoch milliseconds.
- **How to notice:** Create an employee and inspect `created_at`.
- **How it was fixed:** The final code stores a UTC timestamp and returns epoch milliseconds.

### 7. `list_employees` — Pagination and filtering

- **What was wrong:** The starter calculated `skip` as `page * page_size`, counted all employees regardless of the department filter, and did not sort results.
- **How to notice:** Compare the first two pages and compare the total for a department-filtered request.
- **How it was fixed:** The final code uses `(page - 1) * page_size`, counts the filtered query, sorts by `emp_code`, and validates pagination bounds.

### 8. `EmployeeIn` — Input validation

- **What was wrong:** The starter accepted plain strings for employee fields without validating email format, calendar dates, shift-time format, or distinct shift boundaries.
- **How to notice:** Submit an invalid email, impossible date, malformed shift time, or identical shift start and end.
- **How it was fixed:** The final code validates employee codes, email addresses, names, departments, shift times, calendar dates, and distinct shift boundaries. Employee codes must follow the `EMP` prefix with 4–6 digits. Invalid input is rejected with HTTP 422.

### 9. `punch_in` — Unknown employees and status validation

- **What was wrong:** The starter dereferenced the employee record without checking whether it existed, and accepted an unrestricted status string.
- **How to notice:** Punch in using an unknown employee code or an unsupported status.
- **How it was fixed:** The final code returns HTTP 404 for unknown employees and restricts status to `PRESENT`, `WFH`, or `ON_DUTY`.

### 10. `punch_in` — Timestamp handling

- **What was wrong:** The starter used the machine's local timezone, treated timestamp `0` as if no timestamp had been provided, and did not validate or truncate timestamps.
- **How to notice:** Submit an invalid epoch-millisecond value or run under a non-IST system timezone.
- **How it was fixed:** The final code validates epoch milliseconds, converts through UTC, truncates to whole seconds, and uses IST for attendance calculations.

### 11. `punch_in` — Duplicate attendance and response format

- **What was wrong:** A check-then-insert sequence alone is not safe under concurrency, and the starter exposed MongoDB's internal identifier as `id`.
- **How to notice:** Send simultaneous punch-ins for the same employee and date, then inspect a successful response.
- **How it was fixed:** The final code enforces a unique compound index on `(emp_code, date)`, returns HTTP 409 on duplicate-key errors, and omits MongoDB's internal `_id` from the response.

### 12. `list_attendance` — Pagination and memory usage

- **What was wrong:** The starter loaded all matching attendance documents into Python, sorted them there, and sliced the result for pagination.
- **How to notice:** Seed a large collection and request a small page.
- **How it was fixed:** The final code applies filtering, sorting, counting, `skip`, and `limit` in MongoDB.

### 13. `list_attendance` — Validation and response serialization

- **What was wrong:** The starter did not validate date ranges or restrict status values, returned internal identifiers, and returned raw BSON datetimes instead of epoch milliseconds.
- **How to notice:** Submit reversed dates or an invalid status, then inspect the returned attendance timestamps and identifiers.
- **How it was fixed:** The final code validates dates and status, rejects reversed date ranges with HTTP 422, sorts deterministically by date and employee code, omits `_id`, and converts punch and history timestamps to epoch milliseconds.

### 14. `punch_out` — Punch-out fallback query

- **What was wrong:** The fallback query could incorrectly return HTTP 422 when an employee had a punch-in record but no punch-in eligible for the requested punch-out timestamp.
- **How to notice:** Test a punch-out timestamp that occurs before an employee's recorded punch-in.
- **How it was fixed:** The final query considers only punch-in timestamps strictly later than the requested punch-out timestamp when checking this fallback condition.

## Reviewed and Considered Acceptable

- The starter already loads `.env` and supports `MONGO_URI` and `MONGO_DB` environment variables. The localhost URI fallback is a convenience default, not by itself a defect.
- The unique employee-code and employee-date indexes in the final code enforce the relevant uniqueness guarantees.
- Punch-out, regularization, analytics, and explain endpoints were explicitly reserved for implementation in the starter's TODO section. They are implementation tasks rather than pre-existing defects.
- The final `compute_late_minutes()` helper handles overnight shifts, and `compute_overtime()` handles overnight shift-end calculations and the 30-minute threshold. The punch-out endpoint also performs its own overnight-aware overtime calculation.
