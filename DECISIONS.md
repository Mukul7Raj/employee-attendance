# DECISIONS.md

### 1. Indexes
I created:
- `employees`: Unique index on `emp_code` (guarantees employee uniqueness); compound index on `(department, joined_on)` and `(joined_on)` (powers R9 headcount filters without collection scans).
- `attendance_logs`: Unique compound index on `(emp_code, date)` (guarantees at most one log per employee per day); compound index on `(date: -1, emp_code: 1)` (serves attendance listing sort/pagination and date range scans); compound index on `(emp_code: 1, punch_in: -1)` (makes punch-out lookups instant); and index on `(date: 1, late_minutes: 1)` (powers late leaderboard filtering).

*Considered & rejected:* A separate index on `(status)`. Low cardinality (only 5 statuses) makes an index scan inefficient compared to compound indexes prefixed by `date` or `emp_code`.

### 2. Punch-in race
When two identical punch-in requests arrive simultaneously, both evaluate the employee's shift and construct their documents. Both issue `db.attendance_logs.insert_one()`. Because of the unique index on `(emp_code, date)`, MongoDB atomically accepts one insert and rejects the second with a `DuplicateKeyError` (code 11000). The first request returns HTTP 201; the second catches the exception and returns HTTP 409 without needing distributed locks or check-then-insert antipatterns.

### 3. Ties
The leaderboard uses MongoDB's `$rank` window function for standard competition ranking (e.g., ranks 1, 2, 2, 4). The `limit` filter is evaluated against the computed `rank` (`rank <= limit`), not array position. If two employees tie for late minutes at the boundary of `limit`, both share the rank and both are returned, followed by a deterministic secondary sort on `emp_code ASC`.

### 4. Headcount
The department summary aggregation starts from the `employees` collection rather than `attendance_logs`. It filters employees whose `joined_on` is on or before the month's final day, then groups by department with `headcount: {$sum: 1}`. Attendance records are joined via `$lookup`. Employees with zero logs still contribute 1 to department headcount, and empty log arrays yield 0 present days.

### 5. One thing you would change for 100x data
I would partition/shard `attendance_logs` using a hashed compound shard key on `(emp_code, date)`, and pre-aggregate daily/monthly attendance statistics into rolling summary documents during punch-out and regularizations, avoiding scanning millions of raw logs during analytical aggregations.
