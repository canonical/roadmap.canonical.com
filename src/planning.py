"""Capacity Planning module — role-aware team capacity, burn-down curves, and mid-cycle tracking.

All planning data is tied to ``roadmap_item.id`` (stable) so that Jira renames,
status changes, or even soft-deletion never destroy user input.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from .database import get_async_conn, get_db_connection
from .jira_sync import map_tshirt_to_days

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def infer_cycle_dates(cycle_label: str) -> tuple[date, date] | None:
    """Infer start and end dates from a Canonical YY.MM cycle label.

    - .04 cycles: April 1 – September 30 of 20YY
    - .10 cycles: October 1 – March 31 of 20YY+1
    """
    match = re.match(r"^(\d{2})\.(04|10)$", cycle_label)
    if not match:
        return None
    yy = int(match.group(1))
    mm = match.group(2)
    year = 2000 + yy
    if mm == "04":
        start = date(year, 4, 1)
        end = date(year, 9, 30)
    else:  # mm == "10"
        start = date(year, 10, 1)
        end = date(year + 1, 3, 31)
    return start, end


async def auto_populate_cycle_dates() -> int:
    """Update cycle_config rows with NULL start_date or end_date using Canonical convention.

    Returns the number of rows updated.
    """
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute("SELECT cycle FROM cycle_config WHERE start_date IS NULL OR end_date IS NULL")
        rows = await cur.fetchall()
        updated = 0
        for (cycle_label,) in rows:
            dates = infer_cycle_dates(cycle_label)
            if dates:
                start, end = dates
                await cur.execute(
                    "UPDATE cycle_config SET start_date = %s, end_date = %s WHERE cycle = %s",
                    (start, end, cycle_label),
                )
                updated += 1
        if updated:
            await conn.commit()
        return updated


def _cycle_weeks(start: date, end: date) -> list[date]:
    """Return the list of Monday dates between start and end (inclusive)."""
    weeks = []
    current = start
    # Walk back to Monday if start is not Monday
    while current.weekday() != 0:
        current -= timedelta(days=1)
    while current <= end:
        weeks.append(current)
        current += timedelta(days=7)
    return weeks


# ---------------------------------------------------------------------------
# Roles CRUD
# ---------------------------------------------------------------------------


async def list_roles(product_id: int) -> list[dict]:
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT id, name, sort_order, is_default FROM product_role WHERE product_id = %s ORDER BY sort_order, id",
            (product_id,),
        )
        return [
            {"id": r[0], "name": r[1], "sort_order": r[2], "is_default": r[3]}
            for r in await cur.fetchall()
        ]


async def create_role(product_id: int, name: str, sort_order: int = 0, is_default: bool = False, changed_by: str | None = None) -> dict:
    async with get_async_conn() as conn, conn.cursor() as cur:
        # Enforce max 4 roles per product
        await cur.execute("SELECT COUNT(*) FROM product_role WHERE product_id = %s", (product_id,))
        count = (await cur.fetchone())[0]
        if count >= 4:
            raise ValueError("Maximum 4 roles per product")

        # If setting as default, clear other defaults first
        if is_default:
            await cur.execute(
                "UPDATE product_role SET is_default = FALSE WHERE product_id = %s",
                (product_id,),
            )

        await cur.execute(
            "INSERT INTO product_role (product_id, name, sort_order, is_default) VALUES (%s, %s, %s, %s) RETURNING id",
            (product_id, name, sort_order, is_default),
        )
        role_id = (await cur.fetchone())[0]
        await conn.commit()
        await write_audit_log(product_id, "product_role", role_id, "INSERT", None, {"name": name, "sort_order": sort_order, "is_default": is_default}, changed_by)
        return {"id": role_id, "name": name, "sort_order": sort_order, "is_default": is_default}


async def delete_role(role_id: int, changed_by: str | None = None) -> None:
    role = await get_role(role_id)
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM product_role WHERE id = %s", (role_id,))
        await conn.commit()
        await write_audit_log(role["product_id"], "product_role", role_id, "DELETE",
            {"name": role["name"], "sort_order": role["sort_order"], "is_default": role["is_default"]},
            None, changed_by)
        return {"id": role_id, "name": name, "sort_order": sort_order, "is_default": is_default}


async def update_role(role_id: int, name: str | None = None, sort_order: int | None = None, is_default: bool | None = None, changed_by: str | None = None) -> dict:
    async with get_async_conn() as conn, conn.cursor() as cur:
        # Need product_id for default-clear logic
        await cur.execute("SELECT product_id, name, sort_order, is_default FROM product_role WHERE id = %s", (role_id,))
        row = await cur.fetchone()
        if not row:
            raise ValueError(f"Role {role_id} not found")
        product_id, old_name, old_sort, old_default = row[0], row[1], row[2], row[3]

        updates = []
        params = []
        if name is not None:
            updates.append("name = %s")
            params.append(name)
        if sort_order is not None:
            updates.append("sort_order = %s")
            params.append(sort_order)
        if is_default is not None:
            updates.append("is_default = %s")
            params.append(is_default)
            if is_default:
                await cur.execute(
                    "UPDATE product_role SET is_default = FALSE WHERE product_id = %s AND id != %s",
                    (product_id, role_id),
                )
        if not updates:
            return await get_role(role_id)

        params.append(role_id)
        await cur.execute(
            f"UPDATE product_role SET {', '.join(updates)} WHERE id = %s",
            params,
        )
        await conn.commit()

        new_role = await get_role(role_id)
        await write_audit_log(product_id, "product_role", role_id, "UPDATE",
            {"name": old_name, "sort_order": old_sort, "is_default": old_default},
            {"name": new_role["name"], "sort_order": new_role["sort_order"], "is_default": new_role["is_default"]},
            changed_by)
        return new_role


async def get_role(role_id: int) -> dict:
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT id, product_id, name, sort_order, is_default FROM product_role WHERE id = %s",
            (role_id,),
        )
        r = await cur.fetchone()
        if not r:
            raise ValueError(f"Role {role_id} not found")
        return {"id": r[0], "product_id": r[1], "name": r[2], "sort_order": r[3], "is_default": r[4]}


async def delete_role(role_id: int) -> None:
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM product_role WHERE id = %s", (role_id,))
        await conn.commit()


# ---------------------------------------------------------------------------
# Team Members CRUD
# ---------------------------------------------------------------------------


async def list_members(product_id: int) -> list[dict]:
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT m.id, m.name, m.role_id, r.name AS role_name,
                   m.individual_coefficient, m.is_active
            FROM team_member m
            LEFT JOIN product_role r ON r.id = m.role_id
            WHERE m.product_id = %s
            ORDER BY m.name
            """,
            (product_id,),
        )
        return [
            {
                "id": r[0],
                "name": r[1],
                "role_id": r[2],
                "role_name": r[3],
                "individual_coefficient": float(r[4]),
                "is_active": r[5],
            }
            for r in await cur.fetchall()
        ]


async def create_member(product_id: int, name: str, role_id: int | None = None, individual_coefficient: Decimal = Decimal("1.00"), is_active: bool = True) -> dict:
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO team_member (product_id, name, role_id, individual_coefficient, is_active) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (product_id, name, role_id, individual_coefficient, is_active),
        )
        member_id = (await cur.fetchone())[0]
        await conn.commit()
        return {
            "id": member_id,
            "name": name,
            "role_id": role_id,
            "individual_coefficient": float(individual_coefficient),
            "is_active": is_active,
        }


async def update_member(member_id: int, name: str | None = None, role_id: int | None = None, individual_coefficient: Decimal | None = None, is_active: bool | None = None) -> dict:
    async with get_async_conn() as conn, conn.cursor() as cur:
        updates = []
        params = []
        if name is not None:
            updates.append("name = %s")
            params.append(name)
        if role_id is not None:
            updates.append("role_id = %s")
            params.append(role_id)
        if individual_coefficient is not None:
            updates.append("individual_coefficient = %s")
            params.append(individual_coefficient)
        if is_active is not None:
            updates.append("is_active = %s")
            params.append(is_active)
        if not updates:
            return await get_member(member_id)
        params.append(member_id)
        await cur.execute(
            f"UPDATE team_member SET {', '.join(updates)}, updated_at = now() WHERE id = %s",
            params,
        )
        await conn.commit()
        return await get_member(member_id)


async def get_member(member_id: int) -> dict:
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT m.id, m.name, m.role_id, r.name AS role_name,
                   m.individual_coefficient, m.is_active
            FROM team_member m
            LEFT JOIN product_role r ON r.id = m.role_id
            WHERE m.id = %s
            """,
            (member_id,),
        )
        r = await cur.fetchone()
        if not r:
            raise ValueError(f"Member {member_id} not found")
        return {
            "id": r[0],
            "name": r[1],
            "role_id": r[2],
            "role_name": r[3],
            "individual_coefficient": float(r[4]),
            "is_active": r[5],
        }


async def delete_member(member_id: int) -> None:
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM team_member WHERE id = %s", (member_id,))
        await conn.commit()


# ---------------------------------------------------------------------------
# Weekly Availability
# ---------------------------------------------------------------------------


async def get_availability(product_id: int, cycle: str) -> dict:
    """Return availability grid: members × weeks for a cycle."""
    async with get_async_conn() as conn, conn.cursor() as cur:
        # Get cycle dates
        await cur.execute("SELECT start_date, end_date FROM cycle_config WHERE cycle = %s", (cycle,))
        row = await cur.fetchone()
        if not row or not row[0] or not row[1]:
            return {"weeks": [], "members": [], "grid": {}}
        start_date, end_date = row[0], row[1]
        weeks = _cycle_weeks(start_date, end_date)

        # Get members
        await cur.execute(
            "SELECT id, name, role_id, individual_coefficient FROM team_member WHERE product_id = %s AND is_active = TRUE ORDER BY name",
            (product_id,),
        )
        members = [
            {"id": r[0], "name": r[1], "role_id": r[2], "individual_coefficient": float(r[3])}
            for r in await cur.fetchall()
        ]

        # Get availability rows
        await cur.execute(
            """
            SELECT member_id, week_start_date, days_available
            FROM member_weekly_availability mwa
            JOIN team_member tm ON tm.id = mwa.member_id
            WHERE tm.product_id = %s AND mwa.week_start_date BETWEEN %s AND %s
            """,
            (product_id, weeks[0], weeks[-1]),
        )
        grid: dict[int, dict[str, int]] = defaultdict(dict)
        for r in await cur.fetchall():
            grid[r[0]][str(r[1])] = r[2]

        return {
            "weeks": [str(w) for w in weeks],
            "members": members,
            "grid": dict(grid),
        }


async def set_availability(member_id: int, week_start_date: date, days_available: int) -> dict:
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO member_weekly_availability (member_id, week_start_date, days_available)
            VALUES (%s, %s, %s)
            ON CONFLICT (member_id, week_start_date) DO UPDATE SET
                days_available = EXCLUDED.days_available,
                updated_at = now()
            """,
            (member_id, week_start_date, days_available),
        )
        await conn.commit()
        return {"member_id": member_id, "week": str(week_start_date), "days": days_available}


async def bulk_set_availability(entries: list[dict]) -> dict:
    async with get_async_conn() as conn, conn.cursor() as cur:
        for e in entries:
            await cur.execute(
                """
                INSERT INTO member_weekly_availability (member_id, week_start_date, days_available)
                VALUES (%s, %s, %s)
                ON CONFLICT (member_id, week_start_date) DO UPDATE SET
                    days_available = EXCLUDED.days_available,
                    updated_at = now()
                """,
                (e["member_id"], e["week_start_date"], e["days_available"]),
            )
        await conn.commit()
    return {"updated": len(entries)}


# ---------------------------------------------------------------------------
# Planning Config
# ---------------------------------------------------------------------------


async def get_planning_config(product_id: int) -> dict | None:
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT product_id, cycle_id, team_efficiency FROM product_planning_config WHERE product_id = %s",
            (product_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return {"product_id": row[0], "cycle_id": row[1], "team_efficiency": float(row[2])}


async def set_planning_config(product_id: int, cycle_id: str, team_efficiency: Decimal) -> dict:
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO product_planning_config (product_id, cycle_id, team_efficiency)
            VALUES (%s, %s, %s)
            ON CONFLICT (product_id) DO UPDATE SET
                cycle_id = EXCLUDED.cycle_id,
                team_efficiency = EXCLUDED.team_efficiency,
                updated_at = now()
            """,
            (product_id, cycle_id, team_efficiency),
        )
        await conn.commit()
        return {"product_id": product_id, "cycle_id": cycle_id, "team_efficiency": float(team_efficiency)}


# ---------------------------------------------------------------------------
# Epic Estimates
# ---------------------------------------------------------------------------


async def get_epic_estimates(roadmap_item_id: int) -> list[dict]:
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT e.id, e.role_id, r.name AS role_name, e.size_days, e.initial_size_days
            FROM epic_role_estimate e
            JOIN product_role r ON r.id = e.role_id
            WHERE e.roadmap_item_id = %s
            ORDER BY r.sort_order
            """,
            (roadmap_item_id,),
        )
        return [
            {
                "id": r[0],
                "role_id": r[1],
                "role_name": r[2],
                "size_days": r[3],
                "initial_size_days": r[4],
            }
            for r in await cur.fetchall()
        ]


async def set_epic_estimates(roadmap_item_id: int, estimates: list[dict]) -> list[dict]:
    async with get_async_conn() as conn, conn.cursor() as cur:
        # Upsert each estimate
        for est in estimates:
            await cur.execute(
                """
                INSERT INTO epic_role_estimate (roadmap_item_id, role_id, size_days, initial_size_days)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (roadmap_item_id, role_id) DO UPDATE SET
                    size_days = EXCLUDED.size_days,
                    initial_size_days = EXCLUDED.initial_size_days,
                    updated_at = now()
                """,
                (roadmap_item_id, est["role_id"], est["size_days"], est.get("initial_size_days", est["size_days"])),
            )
        await conn.commit()
        return await get_epic_estimates(roadmap_item_id)


# ---------------------------------------------------------------------------
# Epic Selection
# ---------------------------------------------------------------------------


async def get_epic_selection(roadmap_item_id: int, cycle_id: str) -> dict:
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, is_in_roadmap, is_dropped, dropped_at
            FROM epic_cycle_selection
            WHERE roadmap_item_id = %s AND cycle_id = %s
            """,
            (roadmap_item_id, cycle_id),
        )
        row = await cur.fetchone()
        if not row:
            return {"is_in_roadmap": False, "is_dropped": False, "dropped_at": None}
        return {
            "id": row[0],
            "is_in_roadmap": row[1],
            "is_dropped": row[2],
            "dropped_at": row[3].isoformat() if row[3] else None,
        }


async def set_epic_selection(roadmap_item_id: int, cycle_id: str, is_in_roadmap: bool, is_dropped: bool) -> dict:
    async with get_async_conn() as conn, conn.cursor() as cur:
        dropped_at = None
        if is_dropped:
            await cur.execute("SELECT now()")
            dropped_at = (await cur.fetchone())[0]
        await cur.execute(
            """
            INSERT INTO epic_cycle_selection (roadmap_item_id, cycle_id, is_in_roadmap, is_dropped, dropped_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (roadmap_item_id, cycle_id) DO UPDATE SET
                is_in_roadmap = EXCLUDED.is_in_roadmap,
                is_dropped = EXCLUDED.is_dropped,
                dropped_at = EXCLUDED.dropped_at,
                updated_at = now()
            """,
            (roadmap_item_id, cycle_id, is_in_roadmap, is_dropped, dropped_at),
        )
        await conn.commit()
        return await get_epic_selection(roadmap_item_id, cycle_id)


# ---------------------------------------------------------------------------
# Epic Progress (mid-cycle remaining work)
# ---------------------------------------------------------------------------


async def get_epic_progress(roadmap_item_id: int, cycle_id: str) -> dict:
    """Return progress entries for an epic across all weeks of a cycle."""
    async with get_async_conn() as conn, conn.cursor() as cur:
        # Get cycle dates
        await cur.execute("SELECT start_date, end_date FROM cycle_config WHERE cycle = %s", (cycle_id,))
        row = await cur.fetchone()
        if not row or not row[0] or not row[1]:
            return {"weeks": [], "entries": {}}
        weeks = _cycle_weeks(row[0], row[1])

        await cur.execute(
            "SELECT week_start_date, remaining_days FROM epic_weekly_progress WHERE roadmap_item_id = %s",
            (roadmap_item_id,),
        )
        entries = {str(r[0]): r[1] for r in await cur.fetchall()}
        return {"weeks": [str(w) for w in weeks], "entries": entries}


async def set_epic_progress(roadmap_item_id: int, week_start_date: date, remaining_days: int | None, created_by: str | None = None) -> dict:
    async with get_async_conn() as conn, conn.cursor() as cur:
        if remaining_days is None:
            await cur.execute(
                "DELETE FROM epic_weekly_progress WHERE roadmap_item_id = %s AND week_start_date = %s",
                (roadmap_item_id, week_start_date),
            )
        else:
            await cur.execute(
                """
                INSERT INTO epic_weekly_progress (roadmap_item_id, week_start_date, remaining_days, created_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (roadmap_item_id, week_start_date) DO UPDATE SET
                    remaining_days = EXCLUDED.remaining_days,
                    created_by = EXCLUDED.created_by,
                    updated_at = now()
                """,
                (roadmap_item_id, week_start_date, remaining_days, created_by),
            )
        await conn.commit()
        return {"roadmap_item_id": roadmap_item_id, "week": str(week_start_date), "remaining_days": remaining_days}


# ---------------------------------------------------------------------------
# Capacity & Curve Calculations
# ---------------------------------------------------------------------------


async def calculate_capacity(product_id: int, cycle_id: str) -> dict:
    """Return total capacity per week and per role for a product/cycle."""
    async with get_async_conn() as conn, conn.cursor() as cur:
        # Cycle dates
        await cur.execute("SELECT start_date, end_date, team_efficiency FROM cycle_config cc LEFT JOIN product_planning_config ppc ON ppc.cycle_id = cc.cycle AND ppc.product_id = %s WHERE cc.cycle = %s", (product_id, cycle_id))
        row = await cur.fetchone()
        if not row or not row[0] or not row[1]:
            return {"total_weekly": [], "role_weekly": {}, "weeks": []}
        start_date, end_date, efficiency = row[0], row[1], row[2] if row[2] is not None else Decimal("0.60")
        weeks = _cycle_weeks(start_date, end_date)

        # Members with their roles and coefficients
        await cur.execute(
            """
            SELECT m.id, m.role_id, m.individual_coefficient
            FROM team_member m
            WHERE m.product_id = %s AND m.is_active = TRUE
            ORDER BY m.id
            """,
            (product_id,),
        )
        members = {r[0]: {"role_id": r[1], "coefficient": float(r[2])} for r in await cur.fetchall()}

        if not members:
            return {
                "weeks": [str(w) for w in weeks],
                "total_weekly": [0.0] * len(weeks),
                "role_weekly": {},
                "total_capacity": 0.0,
            }

        # Availability for all members across these weeks
        await cur.execute(
            """
            SELECT member_id, week_start_date, days_available
            FROM member_weekly_availability
            WHERE member_id = ANY(%s) AND week_start_date BETWEEN %s AND %s
            """,
            (list(members.keys()), weeks[0], weeks[-1]),
        )
        avail: dict[int, dict[date, int]] = defaultdict(dict)
        for r in await cur.fetchall():
            avail[r[0]][r[1]] = r[2]

        # Role mapping
        await cur.execute("SELECT id, name FROM product_role WHERE product_id = %s", (product_id,))
        roles = {r[0]: r[1] for r in await cur.fetchall()}

        role_weekly: dict[str, list[float]] = {name: [0.0] * len(weeks) for name in roles.values()}
        total_weekly: list[float] = [0.0] * len(weeks)

        for idx, week in enumerate(weeks):
            week_total = 0.0
            for member_id, info in members.items():
                days = avail.get(member_id, {}).get(week, 0)
                if days <= 0:
                    continue
                cap = days * float(efficiency) * info["coefficient"]
                week_total += cap
                role_name = roles.get(info["role_id"])
                if role_name:
                    role_weekly[role_name][idx] += cap
            total_weekly[idx] = round(week_total, 2)

        total_capacity = sum(total_weekly)

        return {
            "weeks": [str(w) for w in weeks],
            "total_weekly": total_weekly,
            "role_weekly": role_weekly,
            "total_capacity": round(total_capacity, 2),
        }


async def calculate_curves(product_id: int, cycle_id: str) -> dict:
    """Return Ideal, Initial, and Expected burn-down curves for a product/cycle.

    Also returns per-role committed-vs-capacity summaries.
    """
    async with get_async_conn() as conn, conn.cursor() as cur:
        # 1. Cycle dates and efficiency
        await cur.execute(
            """
            SELECT cc.start_date, cc.end_date, COALESCE(ppc.team_efficiency, 0.60)
            FROM cycle_config cc
            LEFT JOIN product_planning_config ppc ON ppc.cycle_id = cc.cycle AND ppc.product_id = %s
            WHERE cc.cycle = %s
            """,
            (product_id, cycle_id),
        )
        row = await cur.fetchone()
        if not row or not row[0] or not row[1]:
            return {"weeks": [], "global": {}, "roles": {}, "summary": {}}
        start_date, end_date, efficiency = row[0], row[1], float(row[2])
        weeks = _cycle_weeks(start_date, end_date)
        num_weeks = len(weeks)

        # 2. Roles
        await cur.execute("SELECT id, name FROM product_role WHERE product_id = %s ORDER BY sort_order", (product_id,))
        roles = {r[0]: r[1] for r in await cur.fetchall()}

        # 3. Capacity per week (total and per-role)
        cap_data = await calculate_capacity(product_id, cycle_id)
        total_weekly = cap_data["total_weekly"]
        role_weekly_cap = cap_data["role_weekly"]
        total_capacity = cap_data["total_capacity"]

        # 4. Epics for this cycle
        await cur.execute(
            """
            SELECT r.id, r.jira_key, r.title, r.assignee_name, r.priority, r.t_shirt_size,
                   COALESCE(s.is_in_roadmap, FALSE) AS in_roadmap,
                   COALESCE(s.is_dropped, FALSE) AS dropped,
                   s.dropped_at
            FROM roadmap_item r
            LEFT JOIN epic_cycle_selection s ON s.roadmap_item_id = r.id AND s.cycle_id = %s
            WHERE r.product_id = %s AND %s = ANY(r.tags) AND r.is_deleted = FALSE
            ORDER BY r.priority NULLS LAST, r.rank NULLS LAST, r.title
            """,
            (cycle_id, product_id, cycle_id),
        )
        epics = []
        for r in await cur.fetchall():
            epics.append({
                "id": r[0],
                "jira_key": r[1],
                "title": r[2],
                "assignee_name": r[3],
                "priority": r[4],
                "t_shirt_size": r[5],
                "in_roadmap": r[6],
                "dropped": r[7],
                "dropped_at": r[8],
            })

        # 5. Role estimates for all epics
        epic_ids = [e["id"] for e in epics]
        estimates: dict[int, dict[int, dict]] = defaultdict(dict)
        if epic_ids:
            await cur.execute(
                """
                SELECT roadmap_item_id, role_id, size_days, initial_size_days
                FROM epic_role_estimate
                WHERE roadmap_item_id = ANY(%s)
                """,
                (epic_ids,),
            )
            for r in await cur.fetchall():
                estimates[r[0]][r[1]] = {
                    "size_days": r[2],
                    "initial_size_days": r[3],
                }

        # 6. Progress entries for all epics across these weeks
        progress: dict[int, dict[date, int | None]] = defaultdict(dict)
        if epic_ids:
            await cur.execute(
                """
                SELECT roadmap_item_id, week_start_date, remaining_days
                FROM epic_weekly_progress
                WHERE roadmap_item_id = ANY(%s) AND week_start_date BETWEEN %s AND %s
                """,
                (epic_ids, weeks[0], weeks[-1]),
            )
            for r in await cur.fetchall():
                progress[r[0]][r[1]] = r[2]

        # --- Build curves ---

        # Global committed work
        total_initial_committed = 0
        total_current_committed = 0
        role_initial_committed: dict[str, int] = defaultdict(int)
        role_current_committed: dict[str, int] = defaultdict(int)

        for epic in epics:
            if not epic["in_roadmap"]:
                continue
            for role_id, role_name in roles.items():
                est = estimates.get(epic["id"], {}).get(role_id, {})
                init = est.get("initial_size_days", 0)
                curr = est.get("size_days", 0)
                total_initial_committed += init
                total_current_committed += curr
                role_initial_committed[role_name] += init
                role_current_committed[role_name] += curr

        # Ideal curve (global)
        ideal = [round(total_capacity - sum(total_weekly[:w + 1]), 2) for w in range(num_weeks)]

        # Initial curve (global)
        initial = [round(total_initial_committed - sum(total_weekly[:w + 1]), 2) for w in range(num_weeks)]

        # Expected curve (global)
        expected: list[float] = [0.0] * num_weeks
        expected[0] = round(total_current_committed, 2)
        for w in range(1, num_weeks):
            # Check if any epic has manual progress for this week
            has_manual = False
            manual_sum = 0.0
            for epic in epics:
                if not epic["in_roadmap"] or epic["dropped"]:
                    continue
                rem = progress.get(epic["id"], {}).get(weeks[w])
                if rem is not None:
                    has_manual = True
                # In the spreadsheet logic: if any progress exists for this week,
                # sum all remainders (treating missing as 0 for other epics)
                if rem is not None:
                    manual_sum += rem

            if has_manual:
                expected[w] = round(manual_sum, 2)
            else:
                expected[w] = round(expected[w - 1] - total_weekly[w], 2)

        # Per-role curves (Ideal + Initial only for now)
        role_curves = {}
        for role_id, role_name in roles.items():
            role_cap = role_weekly_cap.get(role_name, [0.0] * num_weeks)
            role_total_cap = sum(role_cap)
            role_ideal = [round(role_total_cap - sum(role_cap[:i + 1]), 2) for i in range(num_weeks)]
            role_initial_curve = [round(role_initial_committed[role_name] - sum(role_cap[:i + 1]), 2) for i in range(num_weeks)]
            role_curves[role_name] = {
                "ideal": role_ideal,
                "initial": role_initial_curve,
                "capacity": round(role_total_cap, 2),
                "committed": role_initial_committed[role_name],
            }

        # Summary
        stretch = (total_initial_committed - total_capacity) / total_capacity if total_capacity > 0 else 0.0
        is_viable = total_initial_committed <= total_capacity * 1.10

        return {
            "weeks": [str(w) for w in weeks],
            "global": {
                "ideal": ideal,
                "initial": initial,
                "expected": expected,
            },
            "roles": role_curves,
            "epics": [
                {
                    "id": e["id"],
                    "jira_key": e["jira_key"],
                    "title": e["title"],
                    "assignee_name": e["assignee_name"],
                    "priority": e["priority"],
                    "t_shirt_size": e["t_shirt_size"],
                    "in_roadmap": e["in_roadmap"],
                    "dropped": e["dropped"],
                    "estimates": [
                        {
                            "role_id": rid,
                            "role_name": roles.get(rid),
                            "size_days": estimates.get(e["id"], {}).get(rid, {}).get("size_days", 0),
                            "initial_size_days": estimates.get(e["id"], {}).get(rid, {}).get("initial_size_days", 0),
                        }
                        for rid in roles
                    ],
                }
                for e in epics
            ],
            "summary": {
                "total_capacity": round(total_capacity, 2),
                "total_committed": total_initial_committed,
                "stretch_pct": round(stretch, 3),
                "is_viable": is_viable,
            },
        }


# ---------------------------------------------------------------------------
# Audit Log
# ---------------------------------------------------------------------------


async def write_audit_log(product_id: int, table_name: str, record_id: int, action: str, old_values: dict | None, new_values: dict | None, changed_by: str | None) -> None:
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO planning_audit_log (product_id, table_name, record_id, action, old_values, new_values, changed_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (product_id, table_name, record_id, action, Jsonb(old_values) if old_values else None, Jsonb(new_values) if new_values else None, changed_by),
        )
        await conn.commit()


async def undo_last_change(product_id: int, changed_by: str | None = None) -> dict:
    """Revert the most recent audit log entry for a product."""
    async with get_async_conn() as conn, conn.cursor() as cur:
        sql = """
            SELECT id, table_name, record_id, action, old_values, new_values
            FROM planning_audit_log
            WHERE product_id = %s
        """
        params = [product_id]
        if changed_by:
            sql += " AND changed_by = %s"
            params.append(changed_by)
        sql += " ORDER BY changed_at DESC LIMIT 1"

        await cur.execute(sql, params)
        row = await cur.fetchone()
        if not row:
            raise ValueError("Nothing to undo")

        audit_id, table_name, record_id, action, old_values, new_values = row

        if action == "UPDATE" and old_values:
            # Build dynamic UPDATE
            sets = []
            vals = []
            for k, v in old_values.items():
                sets.append(f"{k} = %s")
                vals.append(v)
            vals.append(record_id)
            await cur.execute(
                f"UPDATE {table_name} SET {', '.join(sets)} WHERE id = %s",
                vals,
            )
        elif action == "INSERT":
            await cur.execute(f"DELETE FROM {table_name} WHERE id = %s", (record_id,))
        elif action == "DELETE" and old_values:
            # Re-insert
            cols = list(old_values.keys())
            placeholders = ["%s"] * len(cols)
            vals = [old_values[k] for k in cols]
            await cur.execute(
                f"INSERT INTO {table_name} ({', '.join(cols)}) VALUES ({', '.join(placeholders)})",
                vals,
            )

        # Mark as undone
        await cur.execute("DELETE FROM planning_audit_log WHERE id = %s", (audit_id,))
        await conn.commit()
        return {"undone": True, "table": table_name, "record_id": record_id, "action": action}
