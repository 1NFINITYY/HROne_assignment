# DECISIONS.md

## 1. Indexes

I created the following indexes at startup (idempotently via `create_index`):

| Index | Collection | Purpose |
|---|---|---|
| `emp_code` (unique) | `employees` | Enforces uniqueness for concurrent creates; powers O(1) employee lookup. |
| `department` | `employees` | Speeds up department filter in `listEmployees` and analytics. |
| `(emp_code, date)` (unique) | `attendance_logs` | Natural key; makes concurrent punch-ins safe — only one insert wins. Covers the PATCH lookup by `(emp_code, date)`. |
| `(date DESC, emp_code ASC)` | `attendance_logs` | Covers `listAttendance` sort; with a date-range filter MongoDB can do an IXSCAN instead of COLLSCAN. |
| `status` | `attendance_logs` | Supports `listAttendance?status=` filter. |
| `(date ASC, emp_code ASC)` | `attendance_logs` | Supports analytics pipelines that `$match` on a date range and then group by `emp_code`. |

**Considered and rejected:** a separate index on `punch_in` alone. Punch-out finds the most recent open record via `(emp_code, punch_in DESC, punch_out is null)`. The existing compound index on `(emp_code, date)` already narrows the scan to one employee; since there is at most one open record per employee, a further index on `punch_in` would add write overhead for negligible read gain.

## 2. Punch-in race condition

Two identical `POST /attendance/punch-in` requests for `EMP0001` on the same date arrive simultaneously:

1. Both pass Pydantic validation and compute the same attendance date string.
2. Both attempt `db.attendance_logs.insert_one(doc)` — two concurrent inserts into the collection.
3. MongoDB serialises the two inserts at the storage layer. The second one attempts to write a document with a `(emp_code, date)` value that already exists, which violates the unique compound index.
4. MongoDB raises a `DuplicateKeyError` on the second insert, which Python catches and converts to `HTTPException(409)`.

Result: exactly one request gets 201, the other gets 409. No `find_one` is involved (no TOCTOU race).

## 3. Ties in the leaderboard

If two employees have the same `total_late_minutes` at the `limit` boundary (e.g., employees ranked 10th and 11th both have 45 minutes), **both are returned** even though the result contains more than `limit` rows.

This follows the spec: "return every employee whose `rank <= limit`." Standard competition ranking assigns both employees rank 10, so both satisfy `rank <= 10`. The next rank assigned is 12 (the rank that comes after two employees tied at 10). I sort by `total_late_minutes DESC, emp_code ASC` to ensure a deterministic, reproducible order within a tie.

## 4. Headcount with zero-log employees

The department summary aggregation starts from the `employees` collection (not `attendance_logs`), groups by `department`, then uses `$lookup` to join attendance logs. An employee who joined before the end of the month but has no logs simply contributes 1 to `headcount` and 0 to every other metric — the `$lookup` returns an empty array, and all the `$sum`/`$avg` stages treat an empty input as zero/null. This matches R9: employees whose `joined_on <= last day of month` are counted regardless of whether they have any records.

## 5. One thing I would change for 100× the data

I would move the department trend's headcount calculation — currently a Python loop over all employees for each day — into a MongoDB aggregation using a `$lookup` with a range expression or a precomputed lookup stage. At 100× scale, loading tens of thousands of employees into Python memory on every request becomes a bottleneck. A `$lookup` with `$filter` on `joined_on` pushes that work to the database where it can use an index on `joined_on`, and the result can be cached with a short TTL since headcount changes infrequently.
