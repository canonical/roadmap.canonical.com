# Product-Jira mapping

Products are the organisational units shown on the roadmap page. Each product is mapped to one or more Jira projects via **source rules** that determine which Jira issues belong to which product.

## Why a mapping layer?

Jira projects don't map 1:1 to products. Common scenarios:

- One product pulls from **multiple Jira projects** (e.g. LXD uses both the `LXD` and `WD` projects).
- One Jira project is shared by **multiple products**, distinguished by components or labels.
- Some components or labels within a project should be **excluded** from a product.

The `product_jira_source` table provides a flexible, normalised mapping that handles all these cases.

## Mapping rules

Each `product_jira_source` row is a rule that says "issues from Jira project X, matching these filters, belong to product Y". A product can have many rules.

### Filter fields

All filter fields are optional. When multiple filters are set on a single rule, they are **AND-ed** together.

| Filter | Logic |
|--------|-------|
| `include_components` | Issue must have **at least one** of these components |
| `exclude_components` | Issue must **not** have any of these components |
| `include_labels` | Issue must have **at least one** of these labels |
| `exclude_labels` | Issue must **not** have any of these labels |
| `include_teams` | Issue must be owned by **at least one** of these teams |
| `exclude_teams` | Issue must **not** be owned by any of these teams |

### Matching algorithm

During Phase 2 of the sync pipeline, each issue is matched against rules using **first-match-wins**:

```
for each rule in product_jira_source:
    if rule.jira_project_key != issue.project:
        skip
    if include_components is set AND issue has none of them:
        skip
    if exclude_components is set AND issue has any of them:
        skip
    if include_labels is set AND issue has none of them:
        skip
    if exclude_labels is set AND issue has any of them:
        skip
    if include_teams is set AND issue has none of them:
        skip
    if exclude_teams is set AND issue has any of them:
        skip
    → MATCH: assign issue to this rule's product
```

If no rule matches, the issue is assigned to the **Uncategorized** product (auto-seeded on schema creation).

## Design decisions

### Normalised table vs arrays

The mapping could have been stored as arrays on the `product` table (e.g. `primary_projects TEXT[]`). A normalised `product_jira_source` table was chosen because:

- Easier to query — `SELECT DISTINCT jira_project_key FROM product_jira_source` to build JQL
- Easier to CRUD via API — each source is a clean object
- Supports per-source filters without parsing syntax strings

### Integer PK on product

Products use `id SERIAL` as the primary key instead of `name`. This allows products to be renamed without cascading FK updates across `roadmap_item`, `roadmap_snapshot`, etc.

### PUT replaces all sources

The `PUT /api/v1/products/{id}` endpoint deletes all existing `product_jira_source` rows and re-inserts them. This is simpler than PATCH for small cardinality (most products have 1–5 source rules). Individual source management can be added later if needed.

### First-match-wins ordering

The rule evaluation order matches the old spreadsheet convention where the first matching project/filter takes precedence. Since `product_jira_source` rows are loaded without explicit ordering, the match order depends on insertion order (which aligns with the API's `jira_sources` array order).

## JQL construction

The sync pipeline builds its JQL dynamically from the source rules:

```sql
SELECT DISTINCT jira_project_key FROM product_jira_source
```

This means adding a new product with a new Jira project key automatically includes that project in the next sync — no manual JQL editing needed.
