# Reference-dump findings — empirical basis for the MySQL execution validator

*Generated 2026-09-04 by `scripts/mine_reference_dumps.py` from **34
real production dumps** in `Databases/` (1830 tables total).*

These numbers are what the "enterprise-grade" checks in
`app/services/mysql_execution_validator.py` are calibrated against. Re-run the
script if `Databases/` changes; the check comments quote these figures.

## Foreign keys

| Metric | Value |
|---|---|
| Dumps declaring **any** FK constraint | 9/34 (26.5%) |
| FK constraints found | 127 |
| …with explicit `ON DELETE` | 61 (48.0%) |
| …with explicit `ON UPDATE` | 19 (15.0%) |
| Explicit `ON DELETE` actions | `CASCADE` ×60, `SET NULL` ×1 |
| Explicit `ON UPDATE` actions | `CASCADE` ×19 |

When a real dump *does* spell out `ON DELETE`, it is `CASCADE` almost every
time (60 of 61); `SET NULL` shows up once, `RESTRICT`/`NO ACTION`
are never written explicitly. So `most_common_explicit_on_delete = CASCADE` is the
suggestion the check offers, but it stays an **advisory** — CASCADE is not safe
for every relationship.

**Read:** the proprietary style leans on `SET FOREIGN_KEY_CHECKS = 0` plus
application-level integrity — most dumps declare no DB-level FK at all, and of
those that do, the majority leave the referential action at MySQL's implicit
`RESTRICT`. The `fk_no_referential_action` check flags that implicit default so
it becomes a deliberate choice.

## Indexes

| Metric | Value |
|---|---|
| Secondary (non-PK) indexes per table — mean / median | 0.33 / 0.0 |
| Tables with >1 FK | 33 |
| …carrying at least one secondary index per FK | 33 (100.0%) |

## Storage engine

- `innodb`: 1773
- `unspecified`: 17
- `myisam`: 40

InnoDB share: **96.9%**. `non_innodb_table` is an **error** — the
proprietary patterns (row locking, FK support, crash recovery) assume InnoDB.

## Charset / collation

- `latin1`: 808
- `utf8mb4`: 633
- `unspecified`: 17
- `utf8mb3`: 372

utf8mb4 share: **34.6%**; 27/34 dumps
(79.4%) mix more than one charset across their tables — usually a
`latin1` legacy core with newer `utf8mb4` tables bolted on. `charset_inconsistency`
flags that mix; legacy `latin1` / `utf8mb3` get a sharper message (cannot store
Devanagari or emoji).

## Timestamps

| Metric | Value |
|---|---|
| Base (non-junction) tables | 1825 |
| Probable pure-junction tables | 5 |
| Base tables with **both** created + updated stamp | 313 (17.2%) |
| Base tables with **any** timestamp | 32.7% |
| Junction tables with any timestamp | 4/5 (80.0%) |

Dominant column names: **`created_on`** and **`modified_on`** (the `_at` variants
appear too and are accepted). Only **32.7%** of base tables carry any
timestamp at all, which is why `missing_timestamps` is an **advisory**, not an
error — the real corpus is not disciplined about this. The junction-table
exemption is an architectural call, not a data-driven one: the strict
junction heuristic only matched 5 tables here (too small a sample to
lean on), so the check errs toward *not* nagging about link tables.

---

_Machine-readable copy: `app/validators/reference_thresholds.json`._
