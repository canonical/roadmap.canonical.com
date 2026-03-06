#!/usr/bin/env python3
"""Static site generator for the roadmap.

Reads product definitions from products.yaml, fetches matching issues
from Jira, computes color/health status, and writes a static data.json
that the HTML frontend loads client-side.

Usage:
    python build.py                    # fetch from Jira & build
    python build.py --from-db          # export from existing PostgreSQL DB instead
    python build.py --output-dir dist  # change output directory (default: _site)

Environment variables:
    JIRA_URL        Jira server URL (default: https://warthogs.atlassian.net)
    JIRA_USERNAME   Jira username / email
    JIRA_PAT        Jira personal access token
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent / ".env")

# Reuse the existing color logic
sys.path.insert(0, str(Path(__file__).parent))
from src.color_logic import calculate_epic_color

logger = logging.getLogger(__name__)
CYCLE_RE = re.compile(r"^\d{2}\.\d{2}$")

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


@dataclass
class JiraSourceRule:
    product_name: str
    department: str
    jira_project_key: str
    include_components: list[str] = field(default_factory=list)
    exclude_components: list[str] = field(default_factory=list)
    include_labels: list[str] = field(default_factory=list)
    exclude_labels: list[str] = field(default_factory=list)
    include_teams: list[str] = field(default_factory=list)
    exclude_teams: list[str] = field(default_factory=list)


def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def build_rules(config: dict) -> list[JiraSourceRule]:
    rules = []
    for product in config.get("products") or []:
        for src in product.get("jira_sources") or []:
            rules.append(
                JiraSourceRule(
                    product_name=product["name"],
                    department=product.get("department", "Unassigned"),
                    jira_project_key=src["jira_project_key"],
                    include_components=src.get("include_components") or [],
                    exclude_components=src.get("exclude_components") or [],
                    include_labels=src.get("include_labels") or [],
                    exclude_labels=src.get("exclude_labels") or [],
                    include_teams=src.get("include_teams") or [],
                    exclude_teams=src.get("exclude_teams") or [],
                )
            )
    return rules


def match_issue_to_product(
    jira_project_key: str,
    issue_components: list[str],
    issue_labels: list[str],
    issue_teams: list[str],
    rules: list[JiraSourceRule],
) -> JiraSourceRule | None:
    for rule in rules:
        if rule.jira_project_key != jira_project_key:
            continue
        if rule.include_components and not set(rule.include_components) & set(issue_components):
            continue
        if rule.exclude_components and set(rule.exclude_components) & set(issue_components):
            continue
        if rule.include_labels and not set(rule.include_labels) & set(issue_labels):
            continue
        if rule.exclude_labels and set(rule.exclude_labels) & set(issue_labels):
            continue
        if rule.include_teams and not set(rule.include_teams) & set(issue_teams):
            continue
        if rule.exclude_teams and set(rule.exclude_teams) & set(issue_teams):
            continue
        return rule
    return None


# ---------------------------------------------------------------------------
# Jira fetching
# ---------------------------------------------------------------------------


def fetch_from_jira(config: dict, rules: list[JiraSourceRule]) -> list[dict]:
    """Fetch issues from Jira and return processed roadmap items."""
    from jira import JIRA

    jira_url = os.environ.get("JIRA_URL", "https://warthogs.atlassian.net")
    jira_username = os.environ.get("JIRA_USERNAME", "")
    jira_pat = os.environ.get("JIRA_PAT", "")

    if not jira_pat:
        logger.error("JIRA_PAT not set — cannot fetch from Jira")
        sys.exit(1)

    cycles_config = config.get("cycles") or {}
    active_cycles = [c for c, cfg in cycles_config.items() if cfg.get("state") in ("current", "future")]

    project_keys = sorted({r.jira_project_key for r in rules})
    if not project_keys:
        logger.error("No Jira project keys defined in products.yaml")
        sys.exit(1)
    if not active_cycles:
        logger.error("No active cycles (current/future) defined in products.yaml")
        sys.exit(1)

    jql_filter = (config.get("jira") or {}).get("jql_filter", "")
    jql = "project in ({})".format(", ".join(f'"{k}"' for k in project_keys))
    jql += " AND labels in ({})".format(", ".join(active_cycles))
    if jql_filter:
        jql += f" AND {jql_filter}"

    logger.info("Connecting to Jira at %s", jira_url)
    jira = JIRA(server=jira_url, basic_auth=(jira_username, jira_pat))
    logger.info("Running JQL: %s", jql)
    issues = jira.search_issues(jql, maxResults=False)
    logger.info("Fetched %d issues", len(issues))

    # Fetch parent ranks
    parent_keys: set[str] = set()
    for issue in issues:
        parent = (issue.raw.get("fields") or {}).get("parent")
        if isinstance(parent, dict) and parent.get("key"):
            parent_keys.add(parent["key"])

    parent_ranks: dict[str, str] = {}
    fetched_parent_keys = parent_keys.copy()
    for issue in issues:
        if issue.key in parent_keys:
            rank = (issue.raw.get("fields") or {}).get("customfield_10019", "")
            parent_ranks[issue.key] = rank or ""
            fetched_parent_keys.discard(issue.key)

    if fetched_parent_keys:
        CHUNK_SIZE = 100
        keys_list = sorted(fetched_parent_keys)
        for i in range(0, len(keys_list), CHUNK_SIZE):
            chunk = keys_list[i : i + CHUNK_SIZE]
            keys_csv = ", ".join(chunk)
            try:
                parent_issues = jira.search_issues(
                    f"key in ({keys_csv})", maxResults=False, fields="customfield_10019,summary"
                )
                for pi in parent_issues:
                    rank = (pi.raw.get("fields") or {}).get("customfield_10019", "")
                    parent_ranks[pi.key] = rank or ""
            except Exception:
                logger.exception("Failed to fetch parent ranks for chunk")

    # Process issues into roadmap items
    items = []
    for issue in issues:
        fields = issue.raw.get("fields") or {}
        jira_project = issue.key.split("-")[0]

        issue_components = [c["name"] for c in (fields.get("components") or []) if isinstance(c, dict)]
        issue_labels = fields.get("labels") or []

        team_field = fields.get("customfield_10001")
        if isinstance(team_field, dict):
            issue_teams = [team_field.get("name") or team_field.get("value", "")]
        elif isinstance(team_field, list):
            issue_teams = [t.get("name") or t.get("value", "") for t in team_field if isinstance(t, dict)]
        else:
            issue_teams = []

        matched_rule = match_issue_to_product(jira_project, issue_components, issue_labels, issue_teams, rules)
        if not matched_rule:
            continue  # skip issues that don't match any product

        color_status = calculate_epic_color(fields)

        parent = fields.get("parent")
        parent_key = None
        parent_summary = None
        if isinstance(parent, dict):
            parent_key = parent.get("key")
            parent_fields = parent.get("fields") or {}
            parent_summary = parent_fields.get("summary")

        rank = fields.get("customfield_10019") or ""
        parent_rank = parent_ranks.get(parent_key, "") if parent_key else ""

        items.append(
            {
                "jira_key": issue.key,
                "title": fields.get("summary", ""),
                "status": (fields.get("status") or {}).get("name", "Unknown"),
                "tags": issue_labels,
                "product": matched_rule.product_name,
                "department": matched_rule.department,
                "color_status": color_status,
                "url": f"{jira_url}/browse/{issue.key}",
                "parent_key": parent_key,
                "parent_summary": parent_summary,
                "rank": rank,
                "parent_rank": parent_rank,
            }
        )

    logger.info("Processed %d items matching product rules", len(items))
    return items


# ---------------------------------------------------------------------------
# Export from existing PostgreSQL database
# ---------------------------------------------------------------------------


def fetch_from_db(config: dict) -> tuple[list[dict], dict[str, str]]:
    """Export roadmap items from the existing PostgreSQL database."""
    import psycopg

    database_url = os.environ.get(
        "POSTGRESQL_DB_CONNECT_STRING",
        os.environ.get("DATABASE_URL", "postgresql://roadmap:roadmap@localhost:5432/roadmap"),
    )
    jira_url = os.environ.get("JIRA_URL", "https://warthogs.atlassian.net")

    logger.info("Connecting to database")
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT r.jira_key, r.title, r.status, r.tags, "
            "       p.name AS product, p.department, "
            "       r.color_status, r.url, "
            "       r.parent_key, r.parent_summary, r.rank, r.parent_rank "
            "FROM roadmap_item r "
            "JOIN product p ON p.id = r.product_id "
            "ORDER BY r.jira_key"
        )
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()

        # Also fetch frozen cycle items
        cur.execute(
            "SELECT f.jira_key, f.title, f.status, f.tags, "
            "       f.product_name AS product, f.department, "
            "       f.color_status, f.url, "
            "       f.parent_key, f.parent_summary, f.rank, f.parent_rank, "
            "       f.cycle AS frozen_cycle "
            "FROM cycle_freeze_item f "
            "ORDER BY f.jira_key"
        )
        frozen_columns = [desc[0] for desc in cur.description]
        frozen_rows = cur.fetchall()

        # Fetch cycle config
        cur.execute("SELECT cycle, state FROM cycle_config ORDER BY cycle DESC")
        cycle_states = {r[0]: r[1] for r in cur.fetchall()}

    items = []
    seen_keys_by_cycle: dict[str, set[str]] = {}

    # Process live items
    for row in rows:
        item = dict(zip(columns, row, strict=False))
        cs = item.get("color_status")
        if isinstance(cs, str):
            item["color_status"] = json.loads(cs)
        items.append(item)

    # Process frozen items (they take precedence for their cycle)
    for row in frozen_rows:
        item = dict(zip(frozen_columns, row, strict=False))
        cs = item.get("color_status")
        if isinstance(cs, str):
            item["color_status"] = json.loads(cs)
        frozen_cycle = item.pop("frozen_cycle", None)
        if frozen_cycle:
            item["_frozen_cycle"] = frozen_cycle
        items.append(item)

    logger.info("Exported %d items from database", len(items))
    return items, cycle_states


# ---------------------------------------------------------------------------
# Data grouping (shared between both sources)
# ---------------------------------------------------------------------------


def group_items(
    items: list[dict],
    cycles_config: dict[str, dict],
    jira_url: str,
) -> dict:
    """Group items into the structure needed by the frontend.

    Returns a dict ready for JSON serialization:
    {
        "cycles": ["25.10", "25.04", ...],
        "cycle_states": {"25.04": "current", ...},
        "dept_products": {"Dept": ["Product1", ...], ...},
        "products": {
            "Product1": {
                "department": "Dept",
                "cycles": {
                    "25.04": {
                        "Objective Title": [
                            {item}, ...
                        ]
                    }
                }
            }
        },
        "objective_urls": {"Objective Title": "https://..."}
    }
    """
    future_cycles = {c for c, cfg in cycles_config.items() if cfg.get("state") == "future"}
    frozen_cycles = {c for c, cfg in cycles_config.items() if cfg.get("state") == "frozen"}

    # Build dept_products mapping
    dept_products: dict[str, set[str]] = {}
    for item in items:
        dept = item.get("department", "Unassigned")
        prod = item.get("product", "Uncategorized")
        if dept not in ("Unassigned",) and prod not in ("Uncategorized",):
            dept_products.setdefault(dept, set()).add(prod)

    # Sort dept_products
    sorted_dept_products = {
        dept: sorted(prods) for dept, prods in sorted(dept_products.items())
    }

    # Group items: product → cycle → objective → [items]
    products_data: dict[str, dict] = {}
    objective_urls: dict[str, str] = {}
    all_cycles: set[str] = set()

    for item in items:
        product_name = item.get("product", "Uncategorized")
        department = item.get("department", "Unassigned")
        tags = item.get("tags") or []
        item_cycles = [t for t in tags if CYCLE_RE.match(t)]

        if not item_cycles:
            continue

        parent_key = item.get("parent_key")
        parent_summary = item.get("parent_summary")
        if parent_key and parent_summary:
            objective_label = parent_summary
            objective_urls[objective_label] = f"{jira_url}/browse/{parent_key}"
        else:
            objective_label = "No objective"

        # Build the serializable item
        display_item = {
            "jira_key": item["jira_key"],
            "title": item["title"],
            "url": item.get("url", ""),
            "color_status": item.get("color_status") or {},
            "rank": item.get("rank", ""),
            "parent_rank": item.get("parent_rank", ""),
        }

        if product_name not in products_data:
            products_data[product_name] = {"department": department, "cycles": {}}

        frozen_cycle = item.get("_frozen_cycle")

        for c in item_cycles:
            all_cycles.add(c)

            # If this item came from freeze data, only place it in its frozen cycle
            if frozen_cycle and c != frozen_cycle:
                continue

            # If cycle is frozen but this item is from live data, skip
            # (frozen data takes precedence)
            if c in frozen_cycles and not frozen_cycle:
                continue

            cycle_data = products_data[product_name]["cycles"].setdefault(c, {})

            # Compute display color_status with carry-over
            cs = dict(item.get("color_status") or {})

            if c in future_cycles:
                cs = {"health": {"color": "white"}, "carry_over": None}
            else:
                prior_count = sum(1 for lbl in item_cycles if lbl < c)
                cs["carry_over"] = {"color": "purple", "count": prior_count} if prior_count > 0 else None

            display = dict(display_item)
            display["color_status"] = cs

            cycle_data.setdefault(objective_label, []).append(display)

    # Sort within each product/cycle/objective
    for product_name, pdata in products_data.items():
        for cycle_label, obj_map in pdata["cycles"].items():
            # Sort objectives by parent_rank
            sorted_keys = sorted(
                obj_map.keys(),
                key=lambda k: (
                    k == "No objective",
                    min((it.get("parent_rank") or "\xff") for it in obj_map[k]),
                ),
            )
            sorted_obj = OrderedDict()
            for k in sorted_keys:
                sorted_obj[k] = sorted(
                    obj_map[k],
                    key=lambda it: (it.get("rank") or "\xff", it.get("title") or ""),
                )
            pdata["cycles"][cycle_label] = sorted_obj

    cycle_states = {c: cfg.get("state", "current") for c, cfg in cycles_config.items()}

    return {
        "cycles": sorted(all_cycles | set(cycles_config.keys()), reverse=True),
        "cycle_states": cycle_states,
        "dept_products": sorted_dept_products,
        "products": products_data,
        "objective_urls": objective_urls,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Build static roadmap site")
    parser.add_argument("--config", default="products.yaml", help="Path to products.yaml")
    parser.add_argument("--output-dir", default="_site", help="Output directory")
    parser.add_argument("--from-db", action="store_true", help="Export from existing PostgreSQL DB")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    jira_url = os.environ.get("JIRA_URL", "https://warthogs.atlassian.net")
    cycles_config = config.get("cycles") or {}

    if args.from_db:
        items, db_cycle_states = fetch_from_db(config)
        # Merge DB cycle states with config (config takes precedence)
        for c, state in db_cycle_states.items():
            if c not in cycles_config:
                cycles_config[c] = {"state": state}
    else:
        rules = build_rules(config)
        items = fetch_from_jira(config, rules)

    data = group_items(items, cycles_config, jira_url)

    # Write output
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    data_path = out_dir / "data.json"
    data_path.write_text(json.dumps(data, indent=2, default=str))
    logger.info("Wrote %s (%d bytes)", data_path, data_path.stat().st_size)

    # Copy static HTML
    static_src = Path(__file__).parent / "static" / "index.html"
    if static_src.exists():
        shutil.copy2(static_src, out_dir / "index.html")
        logger.info("Copied index.html to %s", out_dir)
    else:
        logger.warning("No static/index.html found — only data.json generated")

    logger.info("Build complete → %s", out_dir)


if __name__ == "__main__":
    main()
