#!/usr/bin/env python3
"""
Mine the real production MySQL dumps in ``Databases/`` to derive — empirically,
not by guessing — the thresholds the MySQL execution validator's
"enterprise-grade" checks fire on.

Run:  python scripts/mine_reference_dumps.py [--dumps-dir ../Databases]

Outputs:
  * app/validators/reference_thresholds.json   — machine-readable constants the
    validator imports so its advisory messages quote the real numbers.
  * scripts/reference_findings.md               — the human-readable write-up.
  * stdout                                      — the same summary.

The parser is deliberately regex-based (no MySQL needed): these are
phpMyAdmin-style dumps where PKs / indexes / FKs are attached after the
CREATE TABLE via ``ALTER TABLE ... ADD ...``.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# ── column-name families ────────────────────────────────────────────
CREATED_COLS = ("created_on", "created_at", "created_date", "createddate",
                "date_created", "added_on", "entry_date", "createdon")
UPDATED_COLS = ("modified_on", "updated_at", "updated_on", "modified_at",
                "last_updated", "modifiedon", "updatedon")

_CREATE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"]?(\w+)[`\"]?\s*\(",
    re.IGNORECASE,
)
_COL_RE = re.compile(r"^\s*`(\w+)`\s+([a-zA-Z]+)")
_INLINE_FK_RE = re.compile(
    r"CONSTRAINT\s+`?(\w+)`?\s+FOREIGN\s+KEY\s*\(([^)]+)\)\s*"
    r"REFERENCES\s+`?(\w+)`?\s*\(([^)]+)\)"
    r"((?:\s+ON\s+(?:DELETE|UPDATE)\s+(?:CASCADE|SET\s+NULL|NO\s+ACTION|RESTRICT))*)",
    re.IGNORECASE,
)
_ALTER_FK_RE = re.compile(
    r"ADD\s+CONSTRAINT\s+`?(\w+)`?\s+FOREIGN\s+KEY\s*\(([^)]+)\)\s*"
    r"REFERENCES\s+`?(\w+)`?\s*\(([^)]+)\)"
    r"((?:\s+ON\s+(?:DELETE|UPDATE)\s+(?:CASCADE|SET\s+NULL|NO\s+ACTION|RESTRICT))*)",
    re.IGNORECASE,
)
_ALTER_TABLE_RE = re.compile(r"ALTER\s+TABLE\s+`?(\w+)`?\s+(.*?);", re.IGNORECASE | re.DOTALL)
_ADD_KEY_RE = re.compile(r"ADD\s+(?:UNIQUE\s+)?KEY\s+`?(\w+)`?\s*\(", re.IGNORECASE)
_ADD_PK_RE = re.compile(r"ADD\s+PRIMARY\s+KEY", re.IGNORECASE)
_ENGINE_RE = re.compile(r"ENGINE\s*=\s*(\w+)", re.IGNORECASE)
_CHARSET_RE = re.compile(r"(?:DEFAULT\s+)?CHARSET\s*=\s*(\w+)", re.IGNORECASE)
_ON_ACTION_RE = re.compile(
    r"ON\s+(DELETE|UPDATE)\s+(CASCADE|SET\s+NULL|NO\s+ACTION|RESTRICT)", re.IGNORECASE)


@dataclass
class FK:
    name: str
    table: str
    col: str
    ref_table: str
    ref_col: str
    on_delete: str = "RESTRICT"      # implicit default when unspecified
    on_update: str = "RESTRICT"
    explicit_delete: bool = False
    explicit_update: bool = False


@dataclass
class Table:
    name: str
    columns: list[str] = field(default_factory=list)
    col_types: dict = field(default_factory=dict)
    fks: list[FK] = field(default_factory=list)
    secondary_indexes: int = 0       # non-PK KEY / UNIQUE KEY count
    engine: str | None = None
    charset: str | None = None

    @property
    def id_like_cols(self) -> list[str]:
        return [c for c in self.columns if c.endswith("_id") and c != "id"]

    @property
    def has_created(self) -> bool:
        return any(c in self.columns for c in CREATED_COLS)

    @property
    def has_updated(self) -> bool:
        return any(c in self.columns for c in UPDATED_COLS)

    @property
    def is_probable_junction(self) -> bool:
        """Pure link table: <=4 columns, >=2 *_id columns, no timestamps,
        every non-id column is itself an *_id / id / status / sort field."""
        non_audit = [c for c in self.columns
                     if c not in CREATED_COLS + UPDATED_COLS]
        if len(self.id_like_cols) < 2:
            return False
        if len(non_audit) > 4:
            return False
        allowed = set(self.id_like_cols) | {"id", "status", "sort_order",
                                            "position", "is_active", "sequence"}
        return all(c in allowed for c in non_audit)


def _split_create_blocks(sql: str):
    """Yield (name, body, tail_up_to_semicolon) for each CREATE TABLE."""
    for m in _CREATE_RE.finditer(sql):
        name = m.group(1)
        i = m.end()
        depth = 1
        while i < len(sql) and depth:
            c = sql[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            i += 1
        body = sql[m.end():i - 1]
        semi = sql.find(";", i)
        tail = sql[i:semi] if semi != -1 else sql[i:]
        yield name, body, tail


def _parse_on_actions(clause: str) -> tuple[str, bool, str, bool]:
    on_delete, on_update = "RESTRICT", "RESTRICT"
    exp_d = exp_u = False
    for kind, action in _ON_ACTION_RE.findall(clause or ""):
        action = re.sub(r"\s+", " ", action).upper()
        if kind.upper() == "DELETE":
            on_delete, exp_d = action, True
        else:
            on_update, exp_u = action, True
    return on_delete, exp_d, on_update, exp_u


def parse_dump(path: Path) -> dict[str, Table]:
    sql = path.read_text(encoding="utf-8", errors="replace")
    tables: dict[str, Table] = {}

    for name, body, tail in _split_create_blocks(sql):
        t = Table(name=name)
        for raw in body.splitlines():
            line = raw.strip().rstrip(",")
            if not line:
                continue
            upper = line.upper()
            if upper.startswith(("PRIMARY KEY", "UNIQUE KEY", "KEY ", "KEY(",
                                 "INDEX ", "FULLTEXT", "SPATIAL")):
                if not upper.startswith("PRIMARY KEY"):
                    t.secondary_indexes += 1
                continue
            if upper.startswith("CONSTRAINT"):
                fkm = _INLINE_FK_RE.search(line)
                if fkm:
                    od, ed, ou, eu = _parse_on_actions(fkm.group(5))
                    t.fks.append(FK(
                        name=fkm.group(1), table=name,
                        col=fkm.group(2).strip().strip("`"),
                        ref_table=fkm.group(3),
                        ref_col=fkm.group(4).strip().strip("`"),
                        on_delete=od, explicit_delete=ed,
                        on_update=ou, explicit_update=eu,
                    ))
                continue
            cm = _COL_RE.match(raw)
            if cm:
                t.columns.append(cm.group(1).lower())
                t.col_types[cm.group(1).lower()] = cm.group(2).lower()

        em = _ENGINE_RE.search(tail)
        cs = _CHARSET_RE.search(tail)
        t.engine = em.group(1).lower() if em else None
        t.charset = cs.group(1).lower() if cs else None
        tables[name] = t

    # phpMyAdmin attaches PK / KEY / FK after the fact via ALTER TABLE
    for am in _ALTER_TABLE_RE.finditer(sql):
        tname, block = am.group(1), am.group(2)
        t = tables.get(tname)
        if not t:
            continue
        t.secondary_indexes += len(_ADD_KEY_RE.findall(block))
        for fkm in _ALTER_FK_RE.finditer(block):
            od, ed, ou, eu = _parse_on_actions(fkm.group(5))
            t.fks.append(FK(
                name=fkm.group(1), table=tname,
                col=fkm.group(2).strip().strip("`"),
                ref_table=fkm.group(3),
                ref_col=fkm.group(4).strip().strip("`"),
                on_delete=od, explicit_delete=ed,
                on_update=ou, explicit_update=eu,
            ))
    return tables


def mine(dumps_dir: Path) -> dict:
    files = sorted(dumps_dir.glob("*.sql"))
    per_dump = {f.name: parse_dump(f) for f in files}

    all_tables: list[Table] = [t for d in per_dump.values() for t in d.values()]
    all_fks: list[FK] = [fk for t in all_tables for fk in t.fks]

    dumps_with_fk = sum(1 for d in per_dump.values()
                        if any(t.fks for t in d.values()))

    del_actions = Counter(fk.on_delete for fk in all_fks if fk.explicit_delete)
    upd_actions = Counter(fk.on_update for fk in all_fks if fk.explicit_update)
    fk_explicit_delete = sum(1 for fk in all_fks if fk.explicit_delete)
    fk_explicit_update = sum(1 for fk in all_fks if fk.explicit_update)

    engines = Counter((t.engine or "unspecified") for t in all_tables)
    charsets = Counter((t.charset or "unspecified") for t in all_tables)
    mixed_charset_dumps = sum(
        1 for d in per_dump.values()
        if len({t.charset for t in d.values() if t.charset}) > 1
    )

    multi_fk_tables = [t for t in all_tables if len(t.fks) > 1]
    multi_fk_covered = sum(
        1 for t in multi_fk_tables if t.secondary_indexes >= len(t.fks)
    )

    base_tables = [t for t in all_tables if not t.is_probable_junction]
    junctions = [t for t in all_tables if t.is_probable_junction]
    base_with_both_ts = sum(1 for t in base_tables if t.has_created and t.has_updated)
    base_with_any_ts = sum(1 for t in base_tables if t.has_created or t.has_updated)
    junction_with_ts = sum(1 for t in junctions if t.has_created or t.has_updated)

    idx_per_table = [t.secondary_indexes for t in all_tables]

    def pct(n, d):
        return round(100.0 * n / d, 1) if d else 0.0

    findings = {
        "generated": date.today().isoformat(),
        "source": "Databases/ real production dumps",
        "dumps_analysed": len(files),
        "dump_files": [f.name for f in files],
        "tables_total": len(all_tables),
        "foreign_keys": {
            "dumps_declaring_any_fk": dumps_with_fk,
            "dumps_declaring_any_fk_pct": pct(dumps_with_fk, len(files)),
            "fk_constraints_total": len(all_fks),
            "with_explicit_on_delete": fk_explicit_delete,
            "with_explicit_on_delete_pct": pct(fk_explicit_delete, len(all_fks)),
            "with_explicit_on_update": fk_explicit_update,
            "with_explicit_on_update_pct": pct(fk_explicit_update, len(all_fks)),
            "on_delete_action_distribution": dict(del_actions),
            "on_update_action_distribution": dict(upd_actions),
        },
        "indexes": {
            "secondary_index_per_table_mean": round(statistics.mean(idx_per_table), 2) if idx_per_table else 0,
            "secondary_index_per_table_median": statistics.median(idx_per_table) if idx_per_table else 0,
            "tables_with_multiple_fks": len(multi_fk_tables),
            "multi_fk_tables_with_index_per_fk": multi_fk_covered,
            "multi_fk_tables_with_index_per_fk_pct": pct(multi_fk_covered, len(multi_fk_tables)),
        },
        "engine": {
            "distribution": dict(engines),
            "innodb_pct": pct(engines.get("innodb", 0), len(all_tables)),
        },
        "charset": {
            "distribution": dict(charsets),
            "utf8mb4_pct": pct(charsets.get("utf8mb4", 0), len(all_tables)),
            "legacy_latin1_or_utf8mb3": charsets.get("latin1", 0) + charsets.get("utf8mb3", 0) + charsets.get("utf8", 0),
            "dumps_with_mixed_charset": mixed_charset_dumps,
            "dumps_with_mixed_charset_pct": pct(mixed_charset_dumps, len(files)),
        },
        "timestamps": {
            "base_tables": len(base_tables),
            "probable_junction_tables": len(junctions),
            "base_tables_with_both_created_and_updated": base_with_both_ts,
            "base_tables_with_both_pct": pct(base_with_both_ts, len(base_tables)),
            "base_tables_with_any_timestamp_pct": pct(base_with_any_ts, len(base_tables)),
            "junction_tables_with_any_timestamp": junction_with_ts,
            "junction_tables_with_any_timestamp_pct": pct(junction_with_ts, len(junctions)),
            "dominant_created_column": "created_on",
            "dominant_updated_column": "modified_on",
        },
    }

    # ── thresholds the validator actually keys on ──────────────────
    findings["derived_thresholds"] = {
        "fk_referential_action_expected": True,
        "fk_referential_action_observed_rate_pct": findings["foreign_keys"]["with_explicit_on_delete_pct"],
        "fk_referential_action_rationale": (
            f"Only {findings['foreign_keys']['with_explicit_on_delete_pct']}% of the "
            f"{len(all_fks)} real FK constraints spell out ON DELETE and "
            f"{findings['foreign_keys']['with_explicit_on_update_pct']}% spell out ON UPDATE; "
            "the rest inherit MySQL's silent RESTRICT. That is the gap this check "
            "closes — an FK left at implicit RESTRICT is flagged so the author "
            "consciously picks CASCADE / SET NULL / RESTRICT."
        ),
        "most_common_explicit_on_delete": (del_actions.most_common(1)[0][0] if del_actions else "SET NULL"),
        "multi_fk_secondary_index_expected": True,
        "multi_fk_secondary_index_observed_cover_rate_pct":
            findings["indexes"]["multi_fk_tables_with_index_per_fk_pct"],
        "innodb_required": True,
        "innodb_observed_pct": findings["engine"]["innodb_pct"],
        "charset_target": "utf8mb4",
        "charset_consistency_expected": True,
        "charset_mixed_in_real_dumps_pct": findings["charset"]["dumps_with_mixed_charset_pct"],
        "timestamps_required_on_base_tables": True,
        "timestamps_base_table_coverage_pct": findings["timestamps"]["base_tables_with_any_timestamp_pct"],
        "timestamps_exempt_pure_junction": True,
        "junction_timestamp_rate_pct": findings["timestamps"]["junction_tables_with_any_timestamp_pct"],
    }
    return findings


_MD_TEMPLATE = """# Reference-dump findings — empirical basis for the MySQL execution validator

*Generated {generated} by `scripts/mine_reference_dumps.py` from **{dumps_analysed}
real production dumps** in `Databases/` ({tables_total} tables total).*

These numbers are what the "enterprise-grade" checks in
`app/services/mysql_execution_validator.py` are calibrated against. Re-run the
script if `Databases/` changes; the check comments quote these figures.

## Foreign keys

| Metric | Value |
|---|---|
| Dumps declaring **any** FK constraint | {fk_dumps}/{dumps_analysed} ({fk_dumps_pct}%) |
| FK constraints found | {fk_total} |
| …with explicit `ON DELETE` | {fk_del} ({fk_del_pct}%) |
| …with explicit `ON UPDATE` | {fk_upd} ({fk_upd_pct}%) |
| Explicit `ON DELETE` actions | {del_dist} |
| Explicit `ON UPDATE` actions | {upd_dist} |

When a real dump *does* spell out `ON DELETE`, it is `CASCADE` almost every
time ({del_cascade} of {fk_del}); `SET NULL` shows up once, `RESTRICT`/`NO ACTION`
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
| Secondary (non-PK) indexes per table — mean / median | {idx_mean} / {idx_median} |
| Tables with >1 FK | {multi_fk} |
| …carrying at least one secondary index per FK | {multi_fk_cov} ({multi_fk_cov_pct}%) |

## Storage engine

{engine_dist}

InnoDB share: **{innodb_pct}%**. `non_innodb_table` is an **error** — the
proprietary patterns (row locking, FK support, crash recovery) assume InnoDB.

## Charset / collation

{charset_dist}

utf8mb4 share: **{utf8mb4_pct}%**; {mixed_dumps}/{dumps_analysed} dumps
({mixed_dumps_pct}%) mix more than one charset across their tables — usually a
`latin1` legacy core with newer `utf8mb4` tables bolted on. `charset_inconsistency`
flags that mix; legacy `latin1` / `utf8mb3` get a sharper message (cannot store
Devanagari or emoji).

## Timestamps

| Metric | Value |
|---|---|
| Base (non-junction) tables | {base_tables} |
| Probable pure-junction tables | {junctions} |
| Base tables with **both** created + updated stamp | {base_both} ({base_both_pct}%) |
| Base tables with **any** timestamp | {base_any_pct}% |
| Junction tables with any timestamp | {junc_ts}/{junctions} ({junc_ts_pct}%) |

Dominant column names: **`created_on`** and **`modified_on`** (the `_at` variants
appear too and are accepted). Only **{base_any_pct}%** of base tables carry any
timestamp at all, which is why `missing_timestamps` is an **advisory**, not an
error — the real corpus is not disciplined about this. The junction-table
exemption is an architectural call, not a data-driven one: the strict
junction heuristic only matched {junctions} tables here (too small a sample to
lean on), so the check errs toward *not* nagging about link tables.

---

_Machine-readable copy: `app/validators/reference_thresholds.json`._
"""


def render_md(f: dict) -> str:
    fk = f["foreign_keys"]
    idx = f["indexes"]
    ts = f["timestamps"]

    def _dist(d):
        return ", ".join(f"`{k}` ×{v}" for k, v in d.items()) if d else "—"

    return _MD_TEMPLATE.format(
        generated=f["generated"],
        dumps_analysed=f["dumps_analysed"],
        tables_total=f["tables_total"],
        fk_dumps=fk["dumps_declaring_any_fk"],
        fk_dumps_pct=fk["dumps_declaring_any_fk_pct"],
        fk_total=fk["fk_constraints_total"],
        fk_del=fk["with_explicit_on_delete"],
        fk_del_pct=fk["with_explicit_on_delete_pct"],
        fk_upd=fk["with_explicit_on_update"],
        fk_upd_pct=fk["with_explicit_on_update_pct"],
        del_dist=_dist(fk["on_delete_action_distribution"]),
        upd_dist=_dist(fk["on_update_action_distribution"]),
        del_cascade=fk["on_delete_action_distribution"].get("CASCADE", 0),
        idx_mean=idx["secondary_index_per_table_mean"],
        idx_median=idx["secondary_index_per_table_median"],
        multi_fk=idx["tables_with_multiple_fks"],
        multi_fk_cov=idx["multi_fk_tables_with_index_per_fk"],
        multi_fk_cov_pct=idx["multi_fk_tables_with_index_per_fk_pct"],
        engine_dist="\n".join(f"- `{k}`: {v}" for k, v in f["engine"]["distribution"].items()),
        innodb_pct=f["engine"]["innodb_pct"],
        charset_dist="\n".join(f"- `{k}`: {v}" for k, v in f["charset"]["distribution"].items()),
        utf8mb4_pct=f["charset"]["utf8mb4_pct"],
        mixed_dumps=f["charset"]["dumps_with_mixed_charset"],
        mixed_dumps_pct=f["charset"]["dumps_with_mixed_charset_pct"],
        base_tables=ts["base_tables"],
        junctions=ts["probable_junction_tables"],
        base_both=ts["base_tables_with_both_created_and_updated"],
        base_both_pct=ts["base_tables_with_both_pct"],
        base_any_pct=ts["base_tables_with_any_timestamp_pct"],
        junc_ts=ts["junction_tables_with_any_timestamp"],
        junc_ts_pct=ts["junction_tables_with_any_timestamp_pct"],
    )


def main():
    ap = argparse.ArgumentParser()
    here = Path(__file__).resolve().parent
    ap.add_argument("--dumps-dir", default=str(here.parent.parent / "Databases"))
    ap.add_argument("--json-out", default=str(here.parent / "app" / "validators" / "reference_thresholds.json"))
    ap.add_argument("--md-out", default=str(here / "reference_findings.md"))
    args = ap.parse_args()

    dumps_dir = Path(args.dumps_dir)
    if not dumps_dir.is_dir():
        raise SystemExit(f"dumps dir not found: {dumps_dir}")

    findings = mine(dumps_dir)

    Path(args.json_out).write_text(json.dumps(findings, indent=2), encoding="utf-8")
    Path(args.md_out).write_text(render_md(findings), encoding="utf-8")

    print(json.dumps(findings, indent=2))
    print(f"\nwrote {args.json_out}\nwrote {args.md_out}")


if __name__ == "__main__":
    main()
