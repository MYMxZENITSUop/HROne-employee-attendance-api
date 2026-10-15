# DECISIONS.md

## 1. Indexes

I created a unique index on `employees.emp_code` to prevent duplicate employee codes and a unique compound index on `attendance_logs (emp_code, date)` to guarantee one attendance record per employee per day. Other indexes support department filtering, date-based attendance queries, employee punch-in lookups, and late-minute analytics. I avoided adding another index for the same unique employee-date guarantee because the existing compound index already enforces it.

## 2. Punch-in race

If two identical punch-in requests arrive simultaneously, both may initially find the employee, but only one insert can succeed because of the unique compound index. The other insert raises `DuplicateKeyError`, which the API converts to `409 Conflict`.

## 3. Ties

The leaderboard uses MongoDB `$rank` on total late minutes and filters by `rank <= limit`. Therefore, all employees tied at the cutoff rank are included, even if the response contains more rows than the requested limit.

## 4. Headcount

Department summary starts from employees who joined before the next month begins. It groups employees by department before looking up attendance logs, so employees without logs still contribute to headcount.

## 5. Scaling

For 100 times more data, I would benchmark representative queries, inspect execution statistics, and review index efficiency before considering archival or partitioning strategies. I would also monitor query latency and collection growth to identify bottlenecks before introducing additional infrastructure.
