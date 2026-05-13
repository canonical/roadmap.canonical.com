# Capacity Planning — Technical Specification

## Status
Draft for engineering review.

## Authors
Maksim Beliaev & OpenCode AI Agent

## Related
- PR020 (Google Sheets predecessor)
- PR030 (Automated Roadmap Spreadsheet Sync)

---

## 1. Overview

This specification describes the **Cycle Roadmap Planner** (capacity planning) feature for the roadmap-web application. It replaces the existing Google Sheets-based capacity planning workflow with a first-class web experience integrated into the existing FastAPI + PostgreSQL + Jinja2 stack.

### 1.1 Goals

1. **Single source of truth**: All planning data lives in PostgreSQL, keyed to stable `jira_key` identifiers.
2. **Role-aware capacity**: Support 1–4 roles per product (team). Detect role-level overcommitment before it becomes a bottleneck.
3. **Burn-down visualization**: Three curves (Ideal, Initial, Expected) computed server-side and rendered via Chart.js.
4. **Mid-cycle correction**: Support manual remaining-work estimates that override the ideal burn-down for the Expected curve.
5. **Data preservation**: User input (estimates, selections, progress) must survive Jira syncs, renames, and even epic removal.

### 1.2 Non-Goals

- **Competency tracking over time**: We do not model when a specific person is available for a specific epic. A person has one role; if they are present, they can work on any epic needing that role.
- **Auto-scheduling**: We do not assign epics to specific weeks. The Initial curve is a simple subtraction of capacity from committed work.
- **Jira write-back**: The "In Roadmap" and "Drop" states are internal to the tool. They do not modify Jira labels or statuses.

---

## 2. Terminology

| Term | Definition |
|------|------------|
| **Cycle** | A 6-month planning period (e.g. `26.04`). Now has explicit `start_date` and `end_date`. |
| **Product** | Synonymous with "team" for this feature. Each product independently plans capacity. |
| **Role** | A competency within a product (e.g. Backend, Frontend, Design, Tech Author). 1–4 per product. |
| **Team Member** | A person assigned to exactly one role in one product. Has an `individual_coefficient`. |
| **Ideal Day** | A day of uninterrupted, 8-hour feature work. All estimates are in ideal days. |
| **T-Shirt Size** | Jira-level epic estimate mapped to ideal days via a Fibonacci table. |
| **Epic Owner** | The Jira `assignee.displayName`. Responsible for completion, not necessarily doing all the work. |
| **Blue Item** | An epic added to the roadmap *after* the initial plan. `initial_size_days = 0`. |
| **In Roadmap** | Boolean flag selecting which epics count toward the current plan. |
| **Dropped** | Boolean flag removing an epic from Initial and Expected curves going forward. |

---

## 3. Jira Integration

### 3.1 Fields Fetched

The existing JQL query in `jira_sync.py` is extended to request these additional fields:

| Jira Field | Internal Name | Usage |
|------------|---------------|-------|
| `assignee.displayName` | `assignee_name` | Epic Owner |
| `priority` | `priority` | Epic priority (High, Medium, Low) |
| `customfield_10040`¹ | `t_shirt_size` | Raw T-shirt size string (S, M, L, etc.) |
| `reporter` | `reporter_name` | Informational |
| `parent.fields.summary` | `parent_summary` | Already fetched |
| `parent.key` | `parent_key` | Already fetched |

¹ The custom field ID for T-shirt size will be configured in `settings.py` (e.g. `JIRA_TSHIRT_FIELD = "customfield_10040"`). If not configured, the sync gracefully skips size extraction.

### 3.2 T-Shirt Size Mapping

When processing raw Jira data, the sync maps the raw string to ideal days:

| T-Shirt | Ideal Days | Ideal Weeks |
|---------|------------|-------------|
| XXS | 5 | 1 |
| XS | 10 | 2 |
| S | 15 | 3 |
| M | 25 | 5 |
| L | 40 | 8 |
| XL | 65 | 13 |
| XXL | 105 | 21 |
| XXXL | 170 | 34 |

Unknown or missing values are mapped to `NULL` and shown as "?" in the UI.

### 3.3 Auto-Initialization of Role Estimates

On every `INSERT` into `roadmap_item` (new epic discovered in Jira):

1. Look up the product's default role (`product_role.is_default = TRUE`).
2. If a default role exists and `t_shirt_size` is known:
   - Insert `epic_role_estimate(roadmap_item_id, role_id, size_days, initial_size_days)`.
   - Both `size_days` and `initial_size_days` = mapped ideal days.
3. If no default role exists, do nothing. The user will manually size.

On `UPDATE` (epic re-synced), `roadmap_item` columns are refreshed, but **no** planning tables are touched.

### 3.4 Soft Delete for Stale Epics

Currently, `sync_jira_data()` hard-deletes stale epics from `roadmap_item`. This destroys planning data.

**Change**: Add `roadmap_item.is_deleted BOOLEAN DEFAULT FALSE`.
- Stale removal sets `is_deleted = TRUE` instead of `DELETE`.
- The UI filters `WHERE is_deleted = FALSE` by default.
- Planning tables remain intact. If the epic reappears in Jira later, `is_deleted` is set back to `FALSE`.

---

## 4. Data Model

### 4.1 Schema Additions

#### `roadmap_item` — additions
```sql
ALTER TABLE roadmap_item
    ADD COLUMN assignee_name VARCHAR(256),
    ADD COLUMN priority      VARCHAR(32),
    ADD COLUMN t_shirt_size  VARCHAR(8),
    ADD COLUMN is_deleted    BOOLEAN NOT NULL DEFAULT FALSE;
```

#### `cycle_config` — additions
```sql
ALTER TABLE cycle_config
    ADD COLUMN start_date DATE,
    ADD COLUMN end_date   DATE;
```

#### `product_role` — new table
One row per role in a product. Max 4.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | |
| `product_id` | INTEGER FK → product | ON DELETE CASCADE |
| `name` | VARCHAR(64) | e.g. "Backend" |
| `sort_order` | INTEGER DEFAULT 0 | UI column order |
| `is_default` | BOOLEAN DEFAULT FALSE | Receives auto-mapped T-shirt size |
| `created_at` | TIMESTAMPTZ | |

Constraint: `UNIQUE (product_id, name)`.
Constraint: `CHECK ((SELECT COUNT(*) FROM product_role WHERE product_id = X) <= 4)`.

#### `team_member` — new table
One row per person.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | |
| `product_id` | INTEGER FK → product | |
| `name` | VARCHAR(128) | Display name |
| `role_id` | INTEGER FK → product_role | NULL = unassigned |
| `individual_coefficient` | DECIMAL(3,2) | Default 1.00. 0.50 = 50% time |
| `is_active` | BOOLEAN | FALSE hides from capacity calc |
| `created_at` / `updated_at` | TIMESTAMPTZ | |

#### `member_weekly_availability` — new table
Sparse grid. One row per member per week.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | |
| `member_id` | INTEGER FK → team_member | |
| `week_start_date` | DATE | Monday of the week |
| `days_available` | INTEGER | 0–5. 5 = full week. NULL not stored. |
| `created_at` / `updated_at` | TIMESTAMPTZ | |

Constraint: `UNIQUE (member_id, week_start_date)`.

Missing weeks = 0 days (member not present that week). This makes it easy to pre-fill a cycle with 5s and let users clear vacation weeks.

#### `product_planning_config` — new table
Singleton per product.

| Column | Type | Notes |
|--------|------|-------|
| `product_id` | INTEGER PK FK → product | |
| `cycle_id` | VARCHAR(16) FK → cycle_config | Current planning cycle |
| `team_efficiency` | DECIMAL(3,2) | Default 0.60 |
| `updated_at` | TIMESTAMPTZ | |

#### `epic_role_estimate` — new table
Per-epic per-role size.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | |
| `roadmap_item_id` | INTEGER FK → roadmap_item | |
| `role_id` | INTEGER FK → product_role | |
| `size_days` | INTEGER | Current estimate (user-overridden) |
| `initial_size_days` | INTEGER | Snapshot at cycle start. 0 = blue item. |
| `created_at` / `updated_at` | TIMESTAMPTZ | |

Constraint: `UNIQUE (roadmap_item_id, role_id)`.

#### `epic_cycle_selection` — new table
Per-epic per-cycle selection state.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | |
| `roadmap_item_id` | INTEGER FK → roadmap_item | |
| `cycle_id` | VARCHAR(16) FK → cycle_config | |
| `is_in_roadmap` | BOOLEAN DEFAULT FALSE | |
| `is_dropped` | BOOLEAN DEFAULT FALSE | |
| `dropped_at` | TIMESTAMPTZ | When dropped |
| `created_at` / `updated_at` | TIMESTAMPTZ | |

Constraint: `UNIQUE (roadmap_item_id, cycle_id)`.

#### `epic_weekly_progress` — new table
Manual mid-cycle remaining work.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | |
| `roadmap_item_id` | INTEGER FK → roadmap_item | |
| `week_start_date` | DATE | |
| `remaining_days` | INTEGER | NULL = no input yet |
| `created_by` | VARCHAR(256) | Email of user |
| `created_at` / `updated_at` | TIMESTAMPTZ | |

Constraint: `UNIQUE (roadmap_item_id, week_start_date)`.

#### `planning_audit_log` — new table
Undo / change tracking.

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | |
| `product_id` | INTEGER FK → product | |
| `table_name` | VARCHAR(64) | Target table |
| `record_id` | INTEGER | PK of changed row |
| `action` | VARCHAR(16) | INSERT / UPDATE / DELETE |
| `old_values` | JSONB | Full row before change |
| `new_values` | JSONB | Full row after change |
| `changed_by` | VARCHAR(256) | User email |
| `changed_at` | TIMESTAMPTZ | |

### 4.2 Referential Integrity Rules

1. **ON DELETE CASCADE** is used for product-scoped tables (`product_role`, `team_member`, `product_planning_config`, `planning_audit_log`). Deleting a product cleans up all its planning data.
2. **ON DELETE CASCADE** is used for cycle-scoped tables (`epic_cycle_selection`, `product_planning_config` when cycle is deleted).
3. **`roadmap_item` soft delete** prevents `ON DELETE CASCADE` from firing when Jira removes an epic. Planning data survives.

---

## 5. Capacity Formulas

### 5.1 Definitions

Let:
- \( P \) = a product
- \( C \) = the cycle being planned, with `start_date` \( S \) and `end_date` \( E \)
- \( W = \lceil (E - S + 1) / 7 \rceil \) = number of weeks in the cycle
- \( w \in [0, W-1] \) = week index. Week 0 starts on \( S \).
- \( R \) = set of roles for product \( P \)
- \( M_r \) = set of active members (`is_active = TRUE`) in role \( r \)
- \( \eta \) = `team_efficiency` (default 0.60)
- \( \alpha_m \) = `individual_coefficient` for member \( m \)
- \( d_{m,w} \) = `days_available` for member \( m \) in week \( w \) (0 if row missing)

### 5.2 Member Capacity (ideal days per week)

\[
c_{m,w} = d_{m,w} \times \eta \times \alpha_m
\]

### 5.3 Role Capacity (ideal days per week)

\[
C_{r,w} = \sum_{m \in M_r} c_{m,w}
\]

### 5.4 Total Capacity (ideal days per week)

\[
C_{\text{total},w} = \sum_{r \in R} C_{r,w}
\]

### 5.5 Cumulative Capacity

\[
\text{CumCap}_w = \sum_{i=0}^{w} C_{\text{total},i}
\]

Note: Capacity is counted in **ideal days**, not calendar days. A full week (5 days) at 0.60 efficiency for a member with coefficient 1.0 yields 3 ideal days.

---

## 6. Burn-Down Curve Formulas

### 6.1 Epic Size Summation

For a given product, cycle, and role:

Let \( E_{\text{in}} \) = set of epics where `epic_cycle_selection.is_in_roadmap = TRUE`.
Let \( E_{\text{drop},w} \) = set of epics where `is_dropped = TRUE` and `dropped_at` week ≤ \( w \).

**Global committed work (Initial snapshot):**

\[
T_{\text{initial}} = \sum_{e \in E_{\text{in}}} \sum_{r \in R} \text{initial_size_days}_{e,r}
\]

**Global current committed work (Expected baseline):**

\[
T_{\text{current}} = \sum_{e \in E_{\text{in}}} \sum_{r \in R} \text{size_days}_{e,r}
\]

### 6.2 Ideal Curve

The Ideal curve represents the team's own capacity burn-down. It is **independent of epic selection**.

\[
\text{Ideal}_w = \text{TotalCycleCapacity} - \text{CumCap}_w
\]

Where:
\[
\text{TotalCycleCapacity} = \text{CumCap}_{W-1}
\]

Properties:
- \(\text{Ideal}_0 = \text{TotalCycleCapacity}\)
- \(\text{Ideal}_{W-1} = 0\)
- Monotonically non-increasing (flat if a week has zero capacity)

### 6.3 Initial Curve

The Initial curve represents the plan as it was at cycle start. It includes only epics that were "In Roadmap" at the start, with their `initial_size_days`. Blue items have `initial_size_days = 0`, so they do not appear.

\[
\text{Initial}_w = T_{\text{initial}} - \text{CumCap}_w
\]

Properties:
- \(\text{Initial}_0 = T_{\text{initial}}\)
- \(\text{Initial}_{W-1} = T_{\text{initial}} - \text{TotalCycleCapacity}\)
- The plan is **viable** if \(\text{Initial}_{W-1} \leq 0\). A 10% stretch (\(\leq 0.10 \times \text{TotalCycleCapacity}\)) is acceptable.
- Dropped epics are **not** removed from Initial. The Initial curve is immutable after the plan is set.

### 6.4 Expected (Actual) Curve

The Expected curve is the most complex. It reflects mid-cycle reality.

**Week 0:**

\[
\text{Expected}_0 = T_{\text{current}}
\]

**Week \( w > 0 \):**

Let \( P_w \) = set of epics in \( E_{\text{in}} \setminus E_{\text{drop},w} \) that have a manual `remaining_days` entry for week \( w \).

\[
\text{Expected}_w =
\begin{cases}
\sum_{e \in P_w} \text{remaining_days}_{e,w} & \text{if } P_w \neq \emptyset \\
\text{Expected}_{w-1} - C_{\text{total},w} & \text{if } P_w = \emptyset \text{ and } \text{Expected}_{w-1} \text{ is defined} \\
T_{\text{current}} - \text{CumCap}_w & \text{otherwise (fallback to ideal trajectory)}
\end{cases}
\]

**Important behavior** (matching the spreadsheet):
- If **any** epic has manual progress entered for week \( w \), the Expected curve for that week becomes the **sum of all manual remaining values** for active epics. Epics without a manual entry for that week are treated as 0 (they have been fully accounted for by the user entering data on other epics, or the user entered 0 for them).
- If **no** epic has manual progress for week \( w \), the Expected curve follows the ideal burn-down rate from the previous week.
- Dropped epics are excluded entirely from \( P_w \).

### 6.5 Role-Specific Curves

All three curves can be computed per-role by restricting sums to a single role:

\[
T_{\text{initial},r} = \sum_{e \in E_{\text{in}}} \text{initial_size_days}_{e,r}
\]

\[
\text{Ideal}_{r,w} = \text{RoleCapacityTotal}_r - \sum_{i=0}^{w} C_{r,i}
\]

\[
\text{Initial}_{r,w} = T_{\text{initial},r} - \sum_{i=0}^{w} C_{r,i}
\]

The Expected curve per-role follows the same conditional logic as global, but using role-specific remaining values. However, since users enter remaining work on the **epic** level (not per-role), the per-role Expected curve is computed by:

1. For each epic, prorate the entered `remaining_days` across roles by the ratio of `size_days` per role.
2. Sum prorated remainders per role.

This is a simplification. If users need per-role mid-cycle tracking, we would need `epic_weekly_progress` to track per-role remainders. **For Phase 1, per-role curves only show Ideal and Initial.** Per-role Expected is deferred until requested.

---

## 7. API Design

### 7.1 New Endpoints

#### Product Roles
```
GET    /api/v1/products/{id}/roles
POST   /api/v1/products/{id}/roles
PUT    /api/v1/products/{id}/roles/{role_id}
DELETE /api/v1/products/{id}/roles/{role_id}
```

#### Team Members
```
GET    /api/v1/products/{id}/members
POST   /api/v1/products/{id}/members
PUT    /api/v1/products/{id}/members/{member_id}
DELETE /api/v1/products/{id}/members/{member_id}
```

#### Weekly Availability
```
GET    /api/v1/products/{id}/availability?cycle={cycle}
POST   /api/v1/products/{id}/availability/bulk  (batch update grid)
PUT    /api/v1/products/{id}/availability/{member_id}/{week}
```

#### Planning Config
```
GET    /api/v1/products/{id}/planning-config
PUT    /api/v1/products/{id}/planning-config
```

#### Epic Estimates
```
GET    /api/v1/epics/{item_id}/estimates
PUT    /api/v1/epics/{item_id}/estimates  (bulk per-role)
```

#### Epic Selection
```
GET    /api/v1/epics/{item_id}/selection?cycle={cycle}
PUT    /api/v1/epics/{item_id}/selection
```

#### Epic Progress
```
GET    /api/v1/epics/{item_id}/progress?cycle={cycle}
POST   /api/v1/epics/{item_id}/progress
```

#### Curves
```
GET    /api/v1/products/{id}/curves?cycle={cycle}
```
Response:
```json
{
  "weeks": ["2026-04-06", "2026-04-13", ...],
  "global": {
    "ideal": [120.0, 114.0, 108.0, ...],
    "initial": [130.0, 124.0, 118.0, ...],
    "expected": [130.0, 120.0, 105.0, ...]
  },
  "roles": {
    "Backend": {
      "ideal": [80.0, 76.0, ...],
      "initial": [90.0, 86.0, ...]
    }
  },
  "summary": {
    "total_capacity": 120,
    "total_committed": 130,
    "stretch_pct": 0.083,
    "is_viable": false
  }
}
```

#### Undo
```
POST   /api/v1/products/{id}/undo
```

### 7.2 Pydantic Schemas

```python
class RoleIn(BaseModel):
    name: str
    sort_order: int = 0
    is_default: bool = False

class TeamMemberIn(BaseModel):
    name: str
    role_id: int | None = None
    individual_coefficient: Decimal = Decimal("1.00")
    is_active: bool = True

class WeeklyAvailabilityBulkIn(BaseModel):
    cycle: str
    entries: list[AvailabilityEntry]

class AvailabilityEntry(BaseModel):
    member_id: int
    week_start_date: date
    days_available: int = Field(ge=0, le=5)

class PlanningConfigIn(BaseModel):
    cycle_id: str
    team_efficiency: Decimal = Field(Decimal("0.60"), ge=Decimal("0.01"), le=Decimal("1.00"))

class EpicEstimateIn(BaseModel):
    estimates: list[RoleEstimateEntry]

class RoleEstimateEntry(BaseModel):
    role_id: int
    size_days: int = Field(ge=0)

class EpicSelectionIn(BaseModel):
    cycle: str
    is_in_roadmap: bool
    is_dropped: bool = False

class EpicProgressIn(BaseModel):
    cycle: str
    week_start_date: date
    remaining_days: int | None = Field(None, ge=0)
```

---

## 8. UI/UX Design

### 8.1 Page Layout

Route: `GET /products/{id}/planning`

```
+--------------------------------------------------+
|  Product Name  |  Cycle: [26.04 ▼]  |  Save OK ✓ |
+--------------------------------------------------+
|  [Configuration]  [Team]  [Epics]  [Progress]      |
+--------------------------------------------------+
```

### 8.2 Configuration Panel

```
Planning Cycle:     [26.04]  (start: 2026-04-06, end: 2026-10-05)
Team Efficiency:    [====|====] 0.60

Roles (max 4):
  [Backend]  [default ★]  [x]
  [Frontend] [set default] [x]
  [+] Add Role
```

HTMX: `PUT /api/v1/products/{id}/planning-config` on slider change.

### 8.3 Team & Availability Grid

```
Team Members:
  Name        | Role      | Coeff | W1  | W2  | W3  | ... | Total
  ---------------------------------------------------------------
  Alice       | Backend   | 1.00  | [5] | [5] | [5] | ... | 15
  Bob         | Frontend  | 0.50  | [5] | [0] | [5] | ... | 10
  [+ Add Member]

Capacity Summary:
  Backend:   15 ideal days  (efficiency adjusted)
  Frontend:  7.5 ideal days
  Total:     22.5 ideal days
```

HTMX: Each cell is a `<input type="number" min="0" max="5">` with `hx-put` on `change` event, `hx-target="closest td"`, `hx-swap="outerHTML"`. The row total and global summary are updated via OOB swap (`hx-swap-oob="true"`).

### 8.4 Epic Selection Table

```
Epics for cycle 26.04:

Key     | Title                | Owner    | Priority | T-Shirt | Backend | Frontend | In | Drop
------------------------------------------------------------------------------------------------
MAAS-1  | Kernel livepatch     | Alice    | High     | M (25)  | [25]    | [0]      | ☑  | ☐
MAAS-2  | UI redesign          | Bob      | High     | L (40)  | [0]     | [40]     | ☑  | ☐
MAAS-3  | Docs refresh         | Carol    | Medium   | S (15)  | [0]     | [0]      | ☐  | ☐
                                                                                              [+]
Backend committed: 25  |  Capacity: 15  ⚠ OVER BY 10
Frontend committed: 40 |  Capacity: 7.5 ⚠ OVER BY 32.5
```

Blue items (not in initial plan) have a light blue row background. Dropped items have a red strikethrough.

The per-role committed-vs-capacity check runs live on every toggle/size change.

### 8.5 Progress Table (Mid-Cycle)

Only visible if the cycle's `start_date` is in the past.

```
Mid-Cycle Progress (remaining ideal days):

Key     | Title                | Initial | W1  | W2  | W3  | ... | Current
-------------------------------------------------------------------------
MAAS-1  | Kernel livepatch     | 25      |     | [20]| [15]| ... | 15
MAAS-2  | UI redesign          | 40      | [38]| [35]|     | ... | 35
MAAS-4  | New feature (blue)   | 0       |     |     | [30]| ... | 30
```

HTMX: `POST /api/v1/epics/{item_id}/progress` on blur. OOB swap updates the "Current" column and the chart.

### 8.6 Burn-Down Chart

Positioned at the top right of the page, sticky during scroll.

Chart.js configuration:
- Type: Line
- X-axis: Week start dates
- Y-axis: Remaining ideal days
- Datasets:
  - Ideal: `borderDash: [5,5]`, color `#666`
  - Initial: solid, color `#06c` (Ubuntu blue)
  - Expected: solid, color `#c7162b` (Ubuntu red)
- Tooltip: Shows exact value on hover.
- Legend: Click to toggle visibility.

### 8.7 Save / Undo Behavior

- **Auto-save**: All inputs auto-save on blur/change. A small "Saved" toast appears.
- **Undo**: `Ctrl+Z` triggers `POST /api/v1/products/{id}/undo`. If successful, the affected component is re-swapped.
- **Conflict**: If another user modified the same row since page load, a yellow banner appears: *"Data changed by another user. Refresh to see latest or overwrite."*

---

## 9. Concurrency & Data Integrity

### 9.1 Optimistic Locking (Lightweight)

Every planning table row has `updated_at`. The UI includes this timestamp in `PUT`/`POST` requests. The backend checks:

```sql
UPDATE epic_role_estimate
SET size_days = %s, updated_at = now()
WHERE id = %s AND updated_at = %s;
```

If `0 rows updated`, the value changed since the user loaded the page. Return `409 Conflict` with current row data. The UI prompts the user to refresh or force overwrite.

### 9.2 Rate Limiting / Debounce

- **Availability grid**: Debounce 300ms on the client. Users tabbing quickly do not fire 50 requests.
- **Bulk API**: The grid supports a `POST /api/v1/products/{id}/availability/bulk` endpoint that accepts a JSON array of changes. A "Save Grid" button can batch all pending changes.
- **Chart refresh**: The chart is re-fetched only after a change settles (500ms debounce) or on explicit "Recalculate".

### 9.3 Data Preservation Guarantees

| Risk | Mitigation |
|------|------------|
| Jira epic deleted | Soft delete `is_deleted` preserves row ID and planning data |
| Jira epic re-created with same key | Sync updates `is_deleted = FALSE`, planning data reattaches |
| Jira epic key changed (rare) | Manual admin script to update `jira_key` if needed. Not automated in Phase 1. |
| Jira custom field renamed | `settings.JIRA_TSHIRT_FIELD` configurable. Missing field → NULL size. |
| User accidentally clears grid | Audit log + undo. Also, pre-fill with 5s at cycle setup so clearing is intentional. |
| Concurrent edit collision | Optimistic locking per-row. User chooses refresh or overwrite. |

---

## 10. Implementation Phases

### Phase 1: Foundation (Schema + Jira Sync)

1. Add `start_date`, `end_date` to `cycle_config`.
2. Add `assignee_name`, `priority`, `t_shirt_size`, `is_deleted` to `roadmap_item`.
3. Create `product_role`, `team_member`, `member_weekly_availability`, `product_planning_config`, `epic_role_estimate`, `epic_cycle_selection`, `epic_weekly_progress`, `planning_audit_log`.
4. Modify `sync_jira_data()` soft-delete logic.
5. Modify `process_raw_jira_data()` to extract new fields and auto-create default role estimates.
6. Add `JIRA_TSHIRT_FIELD` to `settings.py`.
7. Admin API to set cycle dates.
8. Admin UI to set cycle dates.

**Deliverable**: Database schema ready. Jira sync fetches sizes and owners. No user-facing planner yet.

### Phase 2: Configuration & Capacity UI

1. Roles CRUD API + UI.
2. Members CRUD API + UI.
3. Availability grid API + UI (with bulk save).
4. Planning config API + UI (efficiency slider).
5. Team capacity calculation endpoint.
6. Summary badge: committed vs capacity.

**Deliverable**: Users can configure a team and see total capacity. No epic planning yet.

### Phase 3: Epic Planning & Burn-Down

1. Epic selection table API + UI (per-role sizing, In Roadmap, Drop).
2. Blue item support (`initial_size_days = 0` manual toggle).
3. Progress table API + UI.
4. Curve calculation endpoint (`/api/v1/products/{id}/curves`).
5. Chart.js integration with Ideal + Initial + Expected curves.
6. Per-role committed-vs-capacity warnings.

**Deliverable**: Full planning experience. Users can build a roadmap, see burn-down, and do mid-cycle corrections.

### Phase 4: Polish & Safety

1. Audit log triggers on all planning tables.
2. Undo endpoint + Ctrl-Z JS handler.
3. Conflict detection UI.
4. Soft-delete edge cases (epic reappears, key changes).
5. Performance: index `epic_cycle_selection(cycle_id, is_in_roadmap)`, `epic_weekly_progress(week_start_date)`.
6. Tests: capacity math, curve correctness, audit log reversal.

**Deliverable**: Production-ready capacity planner.

---

## 11. Assumptions & Constraints

1. **Team = Product**: For the foreseeable future, capacity planning is scoped to a single product. Cross-product teams are not modeled.
2. **One role per person**: A team member belongs to exactly one role. Split allocations (50% backend, 50% frontend) are handled via `individual_coefficient`, not dual role membership.
3. **5-day work week**: Availability values are integers 0–5. Part-week availability (3.5 days) is not supported. If needed, users can model it via `individual_coefficient`.
4. **Cycle boundaries are inclusive**: The cycle runs from `start_date` (Monday) to `end_date` (Sunday). The number of weeks is `ceil((end_date - start_date + 1) / 7)`.
5. **No work on weekends**: Capacity is zero for Saturday and Sunday implicitly because `days_available` is per-week and represents office days.
6. **Efficiency is global**: `team_efficiency` applies uniformly to all members. Per-member efficiency adjustments use `individual_coefficient`.
7. **Blue items are manual**: The system does **not** auto-detect blue items. The user sets `initial_size_days = 0` (or clicks a "Blue" toggle) for epics added mid-cycle.
8. **Mid-cycle progress is epic-level, not role-level**: When a user enters "15 days remaining" for an epic, it represents the total across all roles. The backend does not track per-role remaining work in Phase 1.
9. **Dropped epics are excluded from Expected**: Once dropped, an epic contributes 0 to Expected for all future weeks. It remains in Initial.
10. **Jira key stability**: The system assumes Jira keys are stable. If an epic is moved to a new key in Jira, planning data is orphaned until manually re-linked.

---

## 12. Edge Cases & Error Handling

| Scenario | Behavior |
|----------|----------|
| Cycle has no dates set | Planner page shows banner: "Set cycle dates to start planning." |
| Product has 0 roles | Planner shows: "Create at least one role." T-shirt auto-mapping is skipped. |
| Product has 0 members | Capacity = 0. All curves are flat at 0. |
| All members have 0 availability for a week | Capacity = 0 for that week. Ideal curve is flat. |
| Epic has no role estimates | Treated as 0 committed work. Shown in table with blank sizes. |
| Total committed > total capacity | Red warning badge. Initial curve ends above 0. User must drop or descope. |
| Expected curve goes negative | Clamp to 0 in UI (all work completed early). Log a debug message. |
| User enters remaining_days > initial size | Allowed. Discoveries happen. The curve will rise. |
| Concurrent edit on same cell | 409 Conflict. UI shows refresh/overwrite prompt. |
| Undo stack empty | 400 Bad Request. Toast: "Nothing to undo." |
| Jira sync removes epic mid-cycle | `is_deleted = TRUE`. Epic disappears from planner table but planning data survives. If user had progress entered, it remains in DB. |

---

## 13. Testing Strategy

### 13.1 Unit Tests (Python)

- **Capacity math**: Given a fixed member/availability setup, assert exact ideal-day totals.
- **Curve math**: Given fixed epic sizes and progress entries, assert exact Ideal/Initial/Expected arrays.
- **T-shirt mapping**: All sizes map correctly; unknown maps to None.
- **Soft delete**: Stale epic → `is_deleted = TRUE`; planning rows survive.
- **Audit log**: Update a value, verify log row, call undo, verify reversion.
- **Conflict**: Simulated concurrent update returns 409.

### 13.2 Integration Tests

- **Jira sync end-to-end**: Mock Jira API returns epics with custom fields. Assert DB state after sync.
- **HTMX round-trip**: Test availability grid POST → DB update → re-rendered HTML fragment.
- **Curve API**: Seed DB with members, epics, progress. Assert JSON response matches expected arrays.

### 13.3 UI Tests (Manual Checklist)

- [ ] Add 4 roles. Verify 5th is rejected.
- [ ] Set efficiency to 0.6. Add member with coefficient 0.5. Enter 5 days for one week. Verify capacity = 1.5 ideal days.
- [ ] Select "In Roadmap" for an epic. Verify committed increases. Badge turns red if over capacity.
- [ ] Enter mid-cycle progress for one epic. Verify Expected curve changes for that week only.
- [ ] Drop an epic. Verify it disappears from Expected but remains in Initial.
- [ ] Press Ctrl-Z after editing a size. Verify value reverts.
- [ ] Open planner in two tabs. Edit in tab A, edit same cell in tab B. Verify conflict banner.

---

## 14. Performance Considerations

| Operation | Target | Strategy |
|-----------|--------|----------|
| Load planner page | < 500ms | All data fetched in 3–4 SQL queries; no N+1 |
| Save single cell | < 200ms | Indexed PK updates; OOB swap only affected cells |
| Recalculate curves | < 100ms | Pre-aggregated capacity per week in memory; no DB scan |
| Jira sync (10k epics) | < 5 min | Existing chunked fetch + bulk upsert |
| Chart render | < 50ms | Chart.js canvas; < 30 data points |

---

## 15. Open Questions (Post-Phase-4)

1. **Per-role mid-cycle progress**: Should we allow users to enter remaining work per role per epic? This would give accurate per-role Expected curves.
2. **Epic Owner assignment in app**: Should the web app allow reassigning the Epic Owner, or is that strictly Jira-driven?
3. **Historical comparison**: Should we snapshot the full planner state (not just Jira state) at cycle freeze time?
4. **Export**: Should the planner data be exportable back to spreadsheet format for stakeholders who prefer sheets?

---

## Appendix A: Exact Spreadsheet Formula Translation

### A.1 Original Sheet Formula for Expected Curve

```
=IF(
    SUMPRODUCT(I3:I79, 1-$D$3:$D$79) > 0,
    SUMPRODUCT(I3:I79, 1-$D$3:$D$79),
    IF(
        SUM(J3:INDIRECT('_internal'!$I$1 & 79)) > 0,
        "",
        H80 - H107 * Configuration!$B$11
    )
)
```

### A.2 Translation to Spec Logic

- `I3:I79` = column of remaining-work values for this week.
- `1-$D$3:$D$79` = `1 - is_dropped`. Dropped epics are excluded.
- `SUMPRODUCT(...) > 0` = "If any epic has manual remaining work entered for this week".
  - Then Expected = sum of remaining work for non-dropped epics.
- `SUM(J3:...)` > 0 = "If there is already a value in a future week" (sheet logic to avoid overwriting).
  - Then leave blank (sheet calculates forward automatically).
- Otherwise: `H80 - H107 * Configuration!$B$11` = previous expected value - weekly capacity.

Our API computes the full array server-side, so "leave blank" is not needed. We always emit a numeric value for every week.

---

## Appendix B: T-Shirt Size to Ideal Days Mapping (Code)

```python
TSHIRT_MAP = {
    "XXS": 5,
    "XS": 10,
    "S": 15,
    "M": 25,
    "L": 40,
    "XL": 65,
    "XXL": 105,
    "XXXL": 170,
}

def map_tshirt_to_days(size_str: str | None) -> int | None:
    if not size_str:
        return None
    return TSHIRT_MAP.get(size_str.upper().strip())
```
