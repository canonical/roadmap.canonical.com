# Managing products

Products represent the organisational units that own roadmap items. Each product belongs to a department and is mapped to one or more Jira projects via **source rules**.

All product management is done through the REST API.

## Create a product

```bash
curl -X POST http://localhost:8000/api/v1/products \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "LXD",
    "department": "Containers",
    "jira_sources": [
      {"jira_project_key": "LXD"},
      {"jira_project_key": "WD", "include_components": ["Anbox/LXD Tribe"]}
    ]
  }'
```

The response includes the assigned `id` and the full product record.

## List all products

```bash
curl http://localhost:8000/api/v1/products
```

Returns all products ordered by department, then name.

## Get a single product

```bash
curl http://localhost:8000/api/v1/products/1
```

## Update a product

`PUT` replaces all fields **and all Jira sources** — it is a full replacement, not a partial patch.

```bash
curl -X PUT http://localhost:8000/api/v1/products/1 \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "LXD",
    "department": "Containers",
    "jira_sources": [
      {"jira_project_key": "LXD", "exclude_components": ["CI"]},
      {"jira_project_key": "WD", "include_components": ["Anbox/LXD Tribe"]},
      {"jira_project_key": "SNAP", "include_labels": ["lxd-related"]}
    ]
  }'
```

## Delete a product

```bash
curl -X DELETE http://localhost:8000/api/v1/products/1
```

Roadmap items that referenced this product will have their `product_id` set to `NULL` (they are **not** deleted).

## Jira source mapping syntax

Each source rule defines how a Jira project maps to the product. All filter fields are optional.

| Field | Type | Description |
|-------|------|-------------|
| `jira_project_key` | `string` | Jira project key (e.g. `LXD`, `MAAS`) — **required** |
| `include_components` | `string[]` | Only include epics with at least one of these components |
| `exclude_components` | `string[]` | Exclude epics with any of these components |
| `include_labels` | `string[]` | Only include epics with at least one of these labels |
| `exclude_labels` | `string[]` | Exclude epics with any of these labels |
| `include_teams` | `string[]` | Only include epics owned by at least one of these teams |
| `exclude_teams` | `string[]` | Exclude epics owned by any of these teams |

When multiple filters are set on a single rule, they are **AND-ed** together.

A product can have **multiple Jira sources**. During sync, each issue is matched against the rules in order — **first match wins**. Issues that don't match any rule land in the `Uncategorized` product.

## Common mapping patterns

| Scenario | API equivalent |
|----------|---------------|
| All epics from project `LXD` | `{"jira_project_key": "LXD"}` |
| Multiple projects for one product | Separate source objects for each key |
| Include specific components | `{"jira_project_key": "WD", "include_components": ["Anbox/LXD Tribe"]}` |
| Exclude a component | `{"jira_project_key": "FR", "exclude_components": ["Toolchains"]}` |
| Include by label | `{"jira_project_key": "PALS", "include_labels": ["Scriptlets", "Starform"]}` |

## After changing products

After creating, updating, or deleting products, **re-sync** to re-process issues with the new mappings:

```bash
curl -X POST http://localhost:8000/api/v1/sync
```
