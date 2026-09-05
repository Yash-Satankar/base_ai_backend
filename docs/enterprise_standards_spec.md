# Enterprise Database Design Standards — Research Spec

**Status: DRAFT FOR REVIEW. No code has been changed as a result of this document.**

## 0. Why this document exists

Every enterprise-check threshold currently in `mysql_execution_validator.py` (see
`app/validators/reference_thresholds.json`) was empirically mined from 34 scraped
real production dumps in `Databases/`. That was the right move to *discover* what
checks matter, but the resulting numbers describe **observed average compliance in
mediocre real systems**, not genuine best practice — e.g. the FK-referential-action
check's own target is calibrated to "48% of real FKs bother to say ON DELETE,"
which is a description of neglect, not a standard to aim for.

Per explicit instruction, `Databases/` is now reference material for **domain
narrative only** — what a financial-ledger or logistics system's entities and
business story look like — not a quality bar. This document researches what
actually distinguishes production database architecture at well-run
organizations, and proposes an updated threshold set and a schema-decomposition
model based on that research. **No code changes are included or implied to have
happened; this is the spec for review before any land.**

Research was done via three parallel literature-review passes (fetched primary
sources directly; citations below are traceable URLs, not paraphrased memory).
Every claim is marked with source(s). Where the research found **no real
consensus** on something the current system assumes, that is reported honestly
rather than papered over — this matters because several such gaps directly
contradict specific values already hard-coded in this codebase (flagged in
Part 2.5).

---

## PART 1 — Research Findings

### 1.1 Foreign key referential actions (ON DELETE / ON UPDATE)

**No source anywhere states a "~100% of FKs should be explicit" statistic.**
That number does not exist in the literature and should not be invented. What
does exist is convergent *qualitative* guidance, plus one hard MySQL-specific
constraint and one important nuance the current single-suggestion design misses:

- The implicit default is a trap, not a neutral choice: MySQL's own docs state
  "Specifying RESTRICT (or NO ACTION) is the same as omitting the ON DELETE or
  ON UPDATE clause" — the silent default is functionally RESTRICT, and nobody
  reading a bare FK can tell whether that was an intentional decision.
  ([MySQL 8.4 Reference Manual](https://dev.mysql.com/doc/refman/8.4/en/create-table-foreign-keys.html))
- **PostgreSQL's official docs give an actual decision framework** MySQL's docs
  don't provide, and it's the right one to encode: *"When the referencing table
  represents something that is a component of what is represented by the
  referenced table and cannot exist independently, then CASCADE could be
  appropriate."* Independent objects → RESTRICT/NO ACTION. Optional
  relationships → SET NULL.
  ([PostgreSQL docs](https://www.postgresql.org/docs/current/ddl-constraints.html))
- **Hard MySQL constraint**: InnoDB rejects `ON DELETE SET DEFAULT` /
  `ON UPDATE SET DEFAULT` outright at DDL time — this is not a style choice,
  it's a rejected statement. Never suggest it.
  ([MySQL docs](https://dev.mysql.com/doc/refman/8.4/en/create-table-foreign-keys.html))
- **Soft-delete tables are a documented, concrete incompatibility with
  CASCADE** — this is the strongest, most actionable finding in this section.
  A soft delete is an `UPDATE ... SET deleted_at = now()`, not a `DELETE`, so
  the FK constraint (and any CASCADE on it) never fires. Brandur Leach
  (Heroku/Stripe-era infrastructure engineer): *"A customer may be soft
  deleted... but we're now back to being able to forget to do the same for its
  invoices."* ([brandur.org/soft-deletion](https://brandur.org/soft-deletion)).
  Corroborated from the ORM-tooling side: *"Do not configure cascade delete in
  the database when soft-deleting entities, as this may cause entities to be
  accidentally really deleted instead of soft-deleted."*
  (laravel-news.com/cascading-soft-deletes)

**Conclusion**: the standard to encode isn't "always suggest CASCADE" (the
current check's only suggestion) — it's *"always be explicit, and pick the
action based on the actual relationship: CASCADE for true
component/owned-child relationships, RESTRICT for independent entities,
RESTRICT (never CASCADE) when the parent is soft-deletable, SET NULL for
optional references, never SET DEFAULT on MySQL."*

### 1.2 Indexing strategy

- **Composite index column ordering is genuinely well-established**, confirmed
  identically by official MySQL docs and Markus Winand's use-the-index-luke.com:
  a multi-column index is only usable via a **leftmost prefix** — `WHERE
  col2=x` cannot use index `(col1, col2, col3)` at all.
  ([MySQL 8.0 docs](https://dev.mysql.com/doc/refman/8.0/en/multiple-column-indexes.html),
  [use-the-index-luke.com](https://use-the-index-luke.com/sql/where-clause/the-equals-operator/concatenated-keys))
  The corollary — **equality columns before range columns** — is the hard rule
  layered on top; pure selectivity-ordering among equality columns is a
  secondary heuristic, not an absolute rule.
- **"Index every FK column" is genuinely contested**, not settled — worth
  stating honestly since the current check does exactly this. SQL Server
  community authorities (Kimberly Tripp, Brent Ozar) lean toward proactive
  indexing. Percona's own engineering blog argues the opposite directly:
  *"We should not indiscriminately create indexes on all FKs because many of
  them will just not be used."*
  ([Percona](https://www.percona.com/blog/should-i-create-an-index-on-foreign-keys-in-postgresql/))
  Karwin's "Index Shotgun" antipattern names the same failure mode. Official
  MySQL docs independently confirm the over-indexing cost: *"unnecessary
  indexes waste space... and add to the cost of inserts, updates, and
  deletes."* ([MySQL docs](https://dev.mysql.com/doc/refman/8.0/en/optimization-indexes.html))
- **Covering indexes should be query-pattern-driven, not default.** Winand's
  explicit guidance: *"Do not design an index for an index-only scan on
  suspicion only... first index without considering the select clause and only
  extend the index if needed."*
  ([use-the-index-luke.com](https://use-the-index-luke.com/sql/clustering/index-only-scan-covering-index))
- **No hard numeric "too many indexes" threshold exists anywhere.** The only
  quantified write-cost figures found are vendor-blog-sourced and
  Postgres-specific (Tiger Data: ~3-5x write amplification, +0.3-0.5x per
  extra index) — directionally useful, not a rule to encode as a hard count.

**Conclusion**: keep "index every FK column" as a *safe default* (it's
defensible, not indefensible — it prevents a real, well-known scan pathology),
but the real gap is the *next* layer up: matching **composite** indexes to
actual query patterns implied by the domain's workflows, which requires
information (L3 workflow/query-pattern data) the current single-column-FK
check doesn't use at all.

### 1.3 Partitioning strategy

- **No hard row-count or GB threshold exists in official docs from either
  engine.** The one quotable official rule of thumb, from PostgreSQL (MySQL's
  own docs give none): *"a rule of thumb is that the size of the table should
  exceed the physical memory of the database server"* — and even Postgres
  hedges this as approximate.
  ([PostgreSQL docs](https://www.postgresql.org/docs/current/ddl-partitioning.html))
- **Real company case studies all describe reactive, operational triggers**,
  not planned thresholds: Notion partitioned after 20B+ rows caused VACUUM
  stalls and transaction-ID wraparound risk
  ([notion.com/blog/sharding-postgres-at-notion](https://www.notion.com/blog/sharding-postgres-at-notion));
  Figma split after IOPS-limit and vacuum-reliability pain at
  multi-terabyte/billion-row scale
  ([figma.com/blog](https://www.figma.com/blog/how-figmas-databases-team-lived-to-tell-the-scale/));
  GitHub partitioned *vertically* (whole tables to separate clusters) to
  reduce incident blast radius, not by a size trigger at all
  ([github.blog](https://github.blog/engineering/infrastructure/partitioning-githubs-relational-databases-scale/)).
- **A hard, unambiguous MySQL-specific restriction with direct design
  implications**: *"InnoDB tables which have or which are referenced by
  foreign keys cannot be partitioned"* — a partitioned table can't declare an
  FK, can't be referenced by one, full stop.
  ([MySQL docs](https://dev.mysql.com/doc/refman/8.0/en/partitioning-limitations-storage-engines.html))
  Any table the generator ever recommends partitioning must first have its FK
  relationships redesigned or dropped — this is a hard gate, not a
  recommendation, and it doesn't exist in PostgreSQL (which removed the
  restriction in v12).
- **Scheme by shape**: RANGE-by-date is the near-universal convention for
  append-only/time-series tables (bulk age-out via `DROP PARTITION`, avoiding
  a slow bulk `DELETE` + VACUUM). For multi-tenant tables, Postgres's own docs
  and Notion's real practice converge on **HASH over LIST** once tenant count
  is large or growing — LIST-per-tenant only makes sense for a small, stable
  tenant set.

**Conclusion**: don't encode a fixed row-count/GB threshold as an "industry
standard" — none exists. Encode partitioning as a judgment call flagged by
operational signals (approaching server RAM in table size; migration/backup
time becoming painful), and hard-gate it against the FK-partitioning
incompatibility.

### 1.4 Naming conventions

- **snake_case, lowercase is the real consensus** — and on MySQL specifically
  this isn't just style, it's a correctness issue: MySQL's identifier
  case-sensitivity is **filesystem-dependent** (case-sensitive on Linux,
  case-insensitive on Windows/macOS) — mixed-case names risk breaking
  cross-platform dev/prod parity.
  ([MySQL 8.4 docs](https://dev.mysql.com/doc/refman/8.4/en/identifier-case-sensitivity.html))
- **"Google's SQL style guide" does not appear to exist** as a public document
  analogous to Google's public Java/Python guides — if this attribution
  appears anywhere in existing docs/prompts, it should be removed as
  unverifiable.
- **Singular vs. plural table names is a genuine, unresolved split, not a
  solved question.** Joe Celko (a primary textbook authority) argues for
  plural/collective names because a table denotes a *set*. The competing
  singular-name convention is largely ORM-driven (one-row-one-object
  symmetry), not textbook-driven. This product's own `_header_all` /
  `_transaction_all` house style sidesteps the debate entirely by using a
  domain-role suffix rather than pluralizing — worth noting in the spec as a
  deliberate, defensible choice, not something the research invalidates.
- **The best concrete, real-world citation for constraint/index naming** is
  **GitLab's actual production standard** (a live document from a real
  company at scale, not a blog opinion): `pk_<table>`, `fk_<table>_<column>_<foreign_table>`,
  `index_<table>_on_<column>`, prefix-first specifically "because it makes it
  easier to identify the type of a given constraint quickly... and group them
  alphabetically."
  ([GitLab docs](https://docs.gitlab.com/development/database/constraint_naming_convention/))
- **No documented standard exists for audit/history table suffixes**
  (`_history`, `_audit`, etc.) — this is a reasonable convention to keep using,
  but should not be presented as an "industry standard," because none was
  found across any vendor or textbook source, including Oracle's own product
  lines being internally inconsistent about it.

### 1.5 Audit/versioning patterns

- **SQL:2011 system-versioned temporal tables are a real standard, but MySQL
  implements none of it.** SQL Server (2016+), MariaDB, and DB2 have real
  (differing) implementations; Oracle uses a proprietary alternative
  (Flashback); **PostgreSQL and MySQL have no native support at all.** An
  independent vendor-neutral survey's conclusion is blunt: *"every other RDBMS
  supports SQL:2011" is a myth.*
  ([illuminatedcomputing.com](https://illuminatedcomputing.com/posts/2019/08/sql2011-survey/))
  Since this product targets MySQL, native temporal tables are not an option —
  the realistic pattern is trigger-based/application-level audit logging.
- **Microsoft's own guidance on *when* to use history/temporal tables is
  scenario-based, not blanket**: it names specific triggers (compliance audit
  of "critical information," point-in-time analysis, anomaly detection, slowly
  changing dimensions) rather than recommending the pattern for every table.
  ([Microsoft Learn](https://learn.microsoft.com/en-us/sql/relational-databases/tables/temporal-table-usage-scenarios))
- **Redgate's audit-design analysis is the most honest treatment found**: it
  lays out the three real MySQL-compatible options (row-versioning, shadow
  tables, generic audit-log tables) and concludes plainly that *"none of them
  is really optimal"* — a genuine, unresolved trade-off, not a solved problem
  with one right answer.
  ([Redgate](https://www.red-gate.com/blog/database-design-for-audit-logging/))
- **Event sourcing** (Fowler) is a real alternative but Fowler himself is
  explicit that it's *not* a default: *"It's not a natural choice"* for most
  systems, and it introduces real complications (replay side-effects, snapshot
  requirements) that only pay off when audit/forensics/point-in-time-replay is
  a genuine business requirement.
  ([martinfowler.com](https://martinfowler.com/eaaDev/EventSourcing.html))

**Conclusion — this is the single most consequential finding for what's
already built (see Part 2.5)**: no source anywhere supports "every mutable
business table needs a paired history/audit companion table" as a blanket
rule. The best-sourced guidance is uniformly **contextual**: apply
history/audit patterns to tables where compliance, forensics, or point-in-time
reporting is an articulable requirement — not universally.

### 1.6 Soft-delete vs. hard-delete

This is the best-sourced, most concrete finding in the whole research pass.

- Brandur Leach's critique (widely discussed, HN/Lobsters front-paged, written
  by a named practitioner with direct Heroku/Stripe-era experience) gives the
  precise mechanism, not just an opinion: soft-delete **silently disables**
  referential-integrity enforcement (CASCADE never fires on an UPDATE),
  every query must remember an extra predicate forever, and — his most
  striking empirical claim — *"never once, in ten-plus years, did anyone at
  any of these places ever actually use soft deletion to undelete
  something."* ([brandur.org/soft-deletion](https://brandur.org/soft-deletion))
  A real, independently-documented production incident (a double-sold-seats
  bug caused by a soft-delete library silently disabled during a migration)
  corroborates the failure mode concretely.
- **Legitimate soft-delete use case**: short-window, user-facing
  undo/trash UX — not a blanket architectural default.
- **GDPR Article 17 ("right to erasure") actively cuts against soft-delete for
  personal data specifically** — a soft-delete flag is definitionally not
  erasure (the row still physically exists, in the live table and in every
  backup). This creates a real, unresolved tension with audit/history
  patterns too: a history table that retains "deleted" personal data
  indefinitely for audit purposes can itself violate an erasure request unless
  personal/identifying fields are separated from non-personal operational
  metadata.
  ([EUR-Lex, Reg. (EU) 2016/679](https://eur-lex.europa.eu/eli/reg/2016/679/oj/eng))

**Conclusion**: neither "always soft-delete" nor "always hard-delete" is
correct. Default to hard-delete for referential integrity; use an explicit,
bounded recoverable-trash mechanism only where undo UX is a real requirement;
treat soft-delete of personal/PII data as a compliance risk requiring explicit
handling, not a safety net.

### 1.7 Multi-tenancy patterns

- **AWS's SaaS Lens (official Well-Architected framework document)** names
  three canonical models — **Silo** (dedicated infra/DB per tenant), **Pool**
  (shared schema + discriminator column), **Bridge** (mix silo/pool
  per-tier/per-tenant-tier) — and is explicit that Pool is the sensible
  starting point for "growth-stage ISVs that prioritize rapid delivery," while
  flagging that Pool "increases reliance on consistent authorization and query
  enforcement" — i.e., every missed `WHERE tenant_id=?` is a cross-tenant leak.
  ([AWS SaaS Lens](https://docs.aws.amazon.com/wellarchitected/latest/saas-lens/silo-isolation.html))
- **Azure's Architecture Center gives a more granular, equally authoritative
  framing**: isolation is a spectrum *per architectural tier*, not one binary
  choice for the whole system — e.g. shared app tier + siloed database is
  presented as normal, not an edge case.
  ([Microsoft Learn](https://learn.microsoft.com/en-us/azure/architecture/guide/multitenant/considerations/tenancy-models))
- **Real large-scale precedent**: Salesforce runs 8,000+ orgs on a single
  shared schema with a tenant/org discriminator on every row (Pool model at
  extreme scale) — described directly by their own chief architect.
  ([InfoQ](https://www.infoq.com/presentations/SalesForce-Multi-Tenant-Architecture-Craig-Weissman/))
  GitLab is, right now, publicly retrofitting an explicit `organization_id`
  sharding key onto every table as the foundation for physical multi-tenancy
  ("Cells") — a live, real example of what *not* designing for this from day
  one costs later.
  ([GitLab docs](https://docs.gitlab.com/development/organization/sharding/))
- **MySQL-specific gap worth flagging**: PostgreSQL has engine-enforced
  Row-Level Security, which mitigates Pool's biggest risk (every query must
  remember the tenant filter) at the database layer. **MySQL has no
  equivalent** — a MySQL Pool-model design is entirely dependent on
  disciplined application-layer enforcement, which is a real, material
  limitation this product's users should be warned about, not silently
  papered over.
- **No source gives a numeric tenant-count threshold** for switching models —
  Citus's own guidance ("millions of tenants" still favors the discriminator
  approach) is a different number than what actually forces most SaaS
  companies off Pool in practice (compliance/noisy-neighbor pressure, usually
  much earlier). Don't encode a specific tenant-count trigger as "the
  standard."

### 1.8 Bounded context as a schema boundary

- **Fowler and Evans tie the boundary to domain-language divergence and team
  ownership (Conway's Law), never to a technical/size metric.** Fowler:
  *"Usually the dominant factor... is human culture, since models act as
  Ubiquitous Language, you need a different model when the language
  changes."* ([martinfowler.com/bliki/BoundedContext](https://martinfowler.com/bliki/BoundedContext.html))
- Fowler's sharpest statement connecting this directly to *databases*:
  *"integration databases should be avoided"* — a shared schema across
  multiple bounded contexts "has to unify what should be separate
  BoundedContexts," creating deep coupling.
  ([martinfowler.com/bliki/IntegrationDatabase](https://martinfowler.com/bliki/IntegrationDatabase.html))
- Conway's Law is the most concrete, actionable driver documented across
  sources: schema/service boundaries tend to mirror team communication
  structure, whether designed that way or not.
  ([martinfowler.com/articles/microservices](https://martinfowler.com/articles/microservices.html))

### 1.9 Is there a table-count threshold for splitting a schema? — **No.**

This directly confirms the suspicion stated in the request, and is worth
stating as plainly as the research supports it: **no authoritative source —
Fowler, Evans, Richardson, Newman, AWS, Azure, or Google Cloud — frames the
decomposition decision by table count, column count, or any structural size
metric.** AWS's own decomposition patterns are literally named "Decompose by
business capability" and "Decompose by subdomain"
([AWS Prescriptive Guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/modernization-decomposing-monoliths/decompose-business-capability.html)) —
domain concepts, not size metrics. Fowler's own "MonolithFirst" essay
explicitly declines to give a crisp trigger: *"I don't feel I have enough
anecdotes yet to get a firm handle on how to decide."*
([martinfowler.com/bliki/MonolithFirst](https://martinfowler.com/bliki/MonolithFirst.html))

The one real numeric data point found is **descriptive, not prescriptive**:
Datadog's real migration organized 326 tables into ~30 schemas by business
domain — a ratio that *emerged* from mapping their actual domains, not a rule
they'd recommend to anyone else.
([Datadog engineering blog](https://www.datadoghq.com/blog/engineering/unwinding-shared-database/))

**Any table-count threshold this spec proposed would be fabricated. None is
proposed** (see Part 2.2 for what's proposed instead).

### 1.10 The "database-per-service" pattern

- Chris Richardson's canonical pattern (microservices.io): keep each service's
  data private, accessed only via its API. Named, honest drawbacks:
  *"Implementing business transactions that span multiple services is not
  straightforward"* and cross-service joins become genuinely hard — which is
  exactly why Saga, API Composition, and CQRS exist as companion patterns.
  ([microservices.io/patterns/data/database-per-service](https://microservices.io/patterns/data/database-per-service.html))
- **Applied at domain/team granularity in real reference architectures, not
  strictly 1:1 per microservice.** Google Cloud's Cymbal Bank blueprint (a
  real, maintained *financial-ledger-adjacent* reference architecture) uses
  exactly two databases for five microservices, grouped by domain (`ledger-db`,
  `accounts-db`) — direct evidence that "database per service" in practice
  usually means "database per bounded context that owns several related
  services."
  ([Google Cloud](https://docs.cloud.google.com/architecture/blueprints/enterprise-application-blueprint/cymbal-bank))
- **Genuine, real debate exists about over-decomposing**: Amazon's own Prime
  Video team publicly reported collapsing a distributed architecture back into
  a monolith for a 90% cost reduction — though follow-up commentary correctly
  notes this was one team's tightly-coupled component boundary problem, not a
  referendum on the pattern generally. Reported (with sourcing caveats,
  primary post is now taken down) via
  [The New Stack](https://thenewstack.io/return-of-the-monolith-amazon-dumps-microservices-for-video-monitoring/).

### 1.11 Cross-schema relationship patterns (no native FK)

All four pattern families anticipated in the request are real, individually
named, and used together in practice — not improvisation:

1. **Denormalized reference copies** — AWS's own decomposition guide gives
   almost exactly the example the request proposed: an `Order` table storing
   `customer_id` (comment: reference only, no FK) plus denormalized
   `customer_first_name`/`email`, with the explicit stated benefit *"If the
   Customer service is down, the Order service remains fully functional."*
   ([AWS Prescriptive Guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/database-decomposition/joins.html))
2. **Event-driven sync + Transactional Outbox** — microservices.io: a service
   can't atomically commit a DB write and publish an event without an outbox
   table in the same transaction, followed by a separate relay process.
   ([microservices.io/patterns/data/transactional-outbox](https://microservices.io/patterns/data/transactional-outbox.html))
3. **API composition** — an API-Gateway-level component joins data from
   multiple services in memory at query time; named drawback: *"some queries
   would result in inefficient, in-memory joins of large datasets."*
   ([microservices.io/patterns/data/api-composition](https://microservices.io/patterns/data/api-composition.html))
4. **CQRS read models** — a maintained, denormalized read-only view kept
   current via subscribed domain events.
   ([microservices.io/patterns/data/cqrs](https://microservices.io/patterns/data/cqrs.html))

**Data ownership is handled by process/documentation, not a database
mechanism** — AWS's own guidance: maintain architecture docs and ADRs
recording service boundaries and data ownership explicitly.
([AWS Prescriptive Guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/database-decomposition/best-practices.html))
**Datadog's real migration is the closest match to what this spec proposes**:
they explicitly removed inter-domain foreign keys as a deliberate step, even
before physically separating databases, "as they sustain undesired
dependencies," and used per-schema ACLs scoped to the owning team as the
actual enforcement mechanism.
([Datadog](https://www.datadoghq.com/blog/engineering/unwinding-shared-database/))
**Shopify goes further still**: they don't enforce FKs at the database layer
*at all*, even within one schema — referential integrity is a
code/model-layer responsibility, enforced by a static boundary-violation
checker (formerly "Wedge," now "Packwerk"), not the database.
([Shopify Engineering](https://shopify.engineering/shopify-made-patterns-in-our-rails-apps))

### 1.12 Modular monolith / schema-per-domain, one instance — and the FK-availability verification requested

**Yes, this is a real, used intermediate pattern** — Datadog's own migration
used exactly this as a deliberate Phase 1 (30 schemas, one physical Postgres
instance) before any physical database splitting. Notably, **even at that
stage, before physical separation forced their hand, they still removed
cross-domain FKs by choice** — treating the schema boundary as a discipline
boundary, not something to exploit just because the engine still allowed it.
Kamil Grzybek's widely-cited modular-monolith reference architecture gives the
same guidance explicitly: *"it's tempting to just query data from another
schema, however, it creates coupling on the database level."*
([kamilgrzybek.com](https://www.kamilgrzybek.com/blog/posts/modular-monolith-integration-styles))

**FK-availability verification, with a needed correction to the original
framing:**

- **PostgreSQL** genuinely has the two-level hierarchy assumed in the
  request: database → schema → table. Cross-*schema* FKs within one database
  work natively. Cross-*database* FKs do not (no engine-enforced mechanism
  across `dblink`/`postgres_fdw`).
- **MySQL does not have this two-level structure** — `CREATE SCHEMA` and
  `CREATE DATABASE` are literal synonyms in MySQL, confirmed identically
  across the 5.7 through 9.7 reference manuals. There is no
  "schema-within-database" tier the way Postgres has one. The real dividing
  line for MySQL is **same server instance vs. different server instance**:
  MySQL/InnoDB *does* support FKs across two different databases on the same
  server (`REFERENCES db2.parent_table(col)`), corroborated by the
  `REFERENCES`-privilege check existing specifically for that cross-owner
  case and a real MySQL engineering worklog (WL#8910) fixing a privilege-check
  bug that only makes sense in a cross-database scenario. Cross-*server*
  (genuinely separate instances) is where FK enforcement is actually lost.

**This is the finding that should correct the original framing in the
request**: "same instance keeps FKs, separate instance loses them" is
directionally right for MySQL, but the boundary sits at *server instance*,
not at a Postgres-style schema/database split (which doesn't exist as two
tiers in MySQL). The stronger, engine-independent recommendation, matching
what Datadog/Shopify/Grzybek actually do: **treat the domain/bounded-context
boundary as a no-FK boundary by convention as soon as it's meant to be
independently ownable — not only once physically forced to by moving to
separate servers.**

---

## PART 2 — Proposed Architecture

### 2.1 Updated enterprise-check thresholds

The current checks live in `app/services/mysql_execution_validator.py`,
calibrated from `app/validators/reference_thresholds.json`. Proposed changes,
one check at a time:

| Check | Current basis | Proposed basis | Change |
|---|---|---|---|
| `_check_fk_referential_actions` | "48% of real FKs are explicit" (Databases/) | Postgres's documented decision framework (§1.1) | **Logic upgrade, not just re-citation.** Replace the single "suggest CASCADE" default with a relationship-aware suggestion: CASCADE for true component/owned-child relationships (e.g. `_details_all`/line-item children of a `_header_all`), RESTRICT for references between independent entities, RESTRICT (never CASCADE, flagged explicitly if CASCADE is present) when the referenced parent carries a soft-delete indicator column, SET NULL for nullable optional FKs. Never suggest SET DEFAULT (MySQL/InnoDB rejects it — hard gate, not a style note). Keep severity=advisory (omission doesn't break functionality) but tighten the message to explain *why* that specific action, not a generic CASCADE nudge. |
| `_check_multi_fk_secondary_index` | "100% of real multi-FK tables index every FK" (Databases/, n=33) | Percona/Winand/official-MySQL-docs: unindexed FK columns force scan-based parent-delete checks and nested-loop joins (§1.2) | Keep "index every FK column" as the safe default (re-grounded in the real technical justification, not survivorship-biased imitation of a 33-table sample) but **add a new layer**: once workflow/query-pattern data is available (see 2.4), recommend composite indexes ordered equality-before-range, matching actual join/filter patterns instead of one single-column index per FK. |
| `_check_missing_timestamps` | "only 33% of real tables have timestamps" (Databases/) | Baseline audit-column practice is cheap and near-universally assumed; distinct from full history/audit tables (§1.5) | Keep the check, but reframe the rationale: `created_on`/`modified_on` are cheap baseline metadata independent of whether a full audit/history table also exists — not "most real schemas skip this so it's only advisory-worthy." |
| **`schema_validator.py` rule 3/4** (`_check_data_preservation` — mandates `_archive_all`/`_life_cycle_all` companions for every non-exempt header table) | Internal design convention, not empirically sourced from Databases/ | **No source found supports a blanket per-table history-table mandate** (§1.5) — Microsoft's own temporal-table usage guidance and Fowler's event-sourcing caveats are both explicitly scenario-based | **This is the highest-impact proposed change — see Part 2.5, item 1.** Rescope this nudge to fire only for tables classified as handling financial, regulated, or otherwise high-criticality data (informed by the L1 domain/industry classification already produced by the pipeline), not every business entity by default. |
| `_check_engine` (InnoDB required) | "96.9% of real tables are InnoDB" (Databases/) | Official MySQL docs: MyISAM has no FK enforcement, no transactions, no crash recovery (§1.3, general MySQL knowledge) | No threshold change — re-ground the citation in the technical reason (which was always the real justification) rather than the observed rate. Keep severity=error. |
| `_check_charset_consistency` (utf8mb4 target) | "79.4% of dumps mix charsets, only 34.6% use utf8mb4" (Databases/) | utf8mb4 is uncontroversial modern MySQL guidance for full Unicode support | No threshold change — re-ground citation, keep severity=advisory. |
| `_check_redundant_indexes` | Structural logic, not threshold-based | Winand's write-amplification note ("the first index makes the greatest difference") + Tiger Data's directional write-amplification figures, used only as supporting color | No change to logic; optional citation addition to the docstring. |
| **New check (proposed)**: partition/FK conflict | — | Hard MySQL restriction: partitioned InnoDB tables can't have or be referenced by FKs (§1.3) | If/when the generator ever recommends partitioning a large append-only table, it must simultaneously flag that table's FK relationships for removal or redesign. Not yet built; flagged here as a real gap if partitioning guidance is ever added to the generator's output. |
| **New check (proposed)**: soft-delete + CASCADE conflict | — | Brandur Leach / Laravel-ecosystem finding (§1.1, §1.6) | Detect a table with a soft-delete indicator (`deleted_at`, `is_deleted`, or a documented soft-delete status value) that is also the parent of an `ON DELETE CASCADE` FK, and flag it — CASCADE from a soft-delete parent gives a false sense of safety since it only fires on a hard delete the pattern is designed to avoid. |

**Explicit non-changes**: naming convention style (`_header_all` etc.) is a
deliberate house style, not something the research invalidates or endorses —
the singular/plural debate has no resolved answer either way (§1.4), so no
change is proposed there beyond removing any "Google's SQL style guide"
attribution if it exists anywhere in prompts/docs, since that source could not
be verified to exist.

### 2.2 Schema-decomposition model for the L1-L8 pipeline

**Do not encode a table-count threshold.** Section 1.9's finding is
unambiguous: no authoritative source frames this decision by size, and
inventing a number (e.g. "split at 40 tables") would be exactly the kind of
empirically-ungrounded rule this whole exercise is meant to move away from.

**What the research does support, mapped onto the existing pipeline:**

The pipeline already produces something structurally very close to a
bounded-context grouping: **L7 "Modules"** (`compile_l4_to_l5_l6_l7` in
`abstraction_pipeline.py`) groups entities and workflows into logical modules
before L8 compiles them into physical tables. This is the natural hook point
— it doesn't need to be invented, it needs to be *used* for a decision it
currently isn't asked to make.

**Proposed model** (opt-in, not automatic — see rationale below):

1. **Default behavior stays single-schema.** This matches Fowler's own
   MonolithFirst guidance directly: don't split before you have proven,
   stable domain boundaries and a reason to enforce them independently.
   Getting the boundary wrong is expensive to undo, and an AI generator
   guessing at DDD boundaries from a short brief is exactly the situation
   where getting it wrong is likely.
2. **Decomposition is triggered by explicit signals in the user's
   requirement, not by table count or an LLM's unprompted judgment call.**
   Concretely: if the L1 Understanding stage detects language indicating
   genuinely separate organizational/product boundaries (e.g., "the billing
   team and the clinical team need to operate independently," "this will
   eventually be split across services," explicit mention of multiple
   products/subsystems with different ownership) — surface this back to the
   user as a question/confirmation before treating it as a decomposition
   signal, rather than silently deciding on the LLM's own interpretation of
   ambiguous language.
3. **When decomposition is confirmed**, the L7 module-compile prompt gains a
   second responsibility beyond grouping: for each pair of modules, classify
   the *strength* of coupling between them using the framing the research
   actually supports (Evans/DDD, operationalized) — does entity A in module X
   reference entity B in module Y as a **required, tightly-consistent
   dependency** (must be correct together, in the same transaction) or as a
   **loose, eventually-consistent reference** (an order references a
   customer, but the order service can function if the customer service is
   briefly stale/unavailable)? Only loose-reference boundaries are eligible to
   become schema boundaries — a required tight dependency across the proposed
   boundary is a signal the module split is wrong, per Evans' original
   framing of bounded-context boundaries needing self-contained transactional
   consistency.
4. **This is intentionally conservative and lower priority than getting
   single-schema generation reliable.** The bulk of this session was spent
   getting a 20-50 table *single* schema to converge reliably; adding
   auto-decomposition before that's solid increases risk without addressing
   the actual current bottleneck (LLM compliance on a large single-schema fix
   pass — a reliability problem, not a design-standards problem). Recommend
   building this as an explicit, separately-flagged Phase 2 feature, not
   folding it into the current showcase-hardening work.

### 2.3 Cross-schema relationship representation standard

When decomposition (2.2) is active, cross-schema references should follow the
convention real organizations actually use (Datadog, Shopify, Grzybek) rather
than the raw technical capability MySQL happens to still offer on the same
server (§1.12):

- **No FK constraint across a schema boundary, by convention — even though
  MySQL technically permits same-server cross-database FKs.** This is a
  discipline choice, not a technical limitation, matching what every real
  precedent found in the research actually does.
- **Represent the reference as a plain column plus a documentation
  comment**, not a `CONSTRAINT`:
  `patient_id INT NOT NULL COMMENT 'References clinical_records.patient_header_all(id) — cross-schema, no FK by design'`
- **Optionally denormalize a small number of frequently-needed display
  fields** (matching AWS's own worked example — e.g. `customer_name` alongside
  `customer_id`), with an explicit staleness caveat documented alongside the
  column.
- **Every table's owning schema/module should be an explicit, carried-through
  piece of metadata** — not just an L7 grouping used once during planning and
  discarded. This should survive into the generated DDL as a per-table header
  comment (`-- Owned by: clinical-records schema`) and into any generated
  documentation (PDF/summary), matching AWS's and Datadog's own guidance that
  ownership is tracked via documentation/process, not a database mechanism.
- **For genuine cross-schema query needs** (a dashboard needing data from two
  schemas), the generator should **document** the standard pattern
  (event-driven sync to a local read-model table, or API composition at the
  application layer) as guidance in the deliverable — it should not attempt
  to generate message-queue/event-bus infrastructure itself. That's a real
  scope boundary: this product generates schemas, not distributed-systems
  infrastructure.

### 2.4 Required pipeline changes

Concretely, if 2.2/2.3 are approved and built (separately from the threshold
changes in 2.1, which can land independently and immediately):

- **`abstraction_pipeline.py`**: L7 module-compile prompt (`compile_l4_to_l5_l6_l7`)
  needs a new instruction to classify inter-module coupling strength
  (tight/required vs. loose/optional), but *only* when decomposition mode is
  active — the default single-schema path should be unaffected, so this is an
  additive branch, not a rewrite of the existing prompt.
- **Blueprint schema** (`app/schemas/blueprint_schema.py`): needs an optional
  `schema_name` field per module, defaulting to a single implicit schema when
  decomposition isn't triggered — backward compatible with every existing
  test and the current single-schema generation path.
- **Batch generator** (`planner_service.py`): when decomposition is active,
  needs to emit DDL grouped per schema (either separate `CREATE DATABASE`
  blocks or separate output artifacts per schema) and must downgrade any
  relationship crossing a schema boundary from a `CONSTRAINT ... FOREIGN KEY`
  to a plain commented reference column per 2.3 — this is real stitching-logic
  work, not a small tweak.
- **`schema_refiner.py`**: the entire targeted-fix mechanism built this
  session (`_iter_table_blocks`, `_attribute`, `_do_targeted_iteration`,
  splice-and-verify) currently assumes one DDL string. **This was explicitly
  anticipated in the original request and is confirmed necessary by this
  research**: with decomposition, fixes must be scoped per schema-module (run
  `SchemaValidator` + `mysql_execution_validator` separately per schema,
  splice only within that schema's DDL), and table names would need
  schema-qualification (`schema.table`) since two different schemas could
  legitimately contain same-named tables. This is a meaningful refactor of
  the attribution/splice layer, not a parameter change.
- **`mysql_execution_validator.py`**: currently validates one DDL string
  against one ephemeral/DSN database. Multi-schema validation needs either
  (a) one database per schema-module inside the same MySQL server/testcontainer,
  validated independently, or (b) a single database with multiple MySQL
  "schemas" (= databases, per §1.12) created and cross-validated together. It
  would also need a **new structural check** enforcing the no-FK-by-convention
  boundary rule from 2.3 (flag any FK-style constraint that crosses a
  documented schema boundary as a violation, not just an advisory).

### 2.5 Explicit conflicts with what's already built

Flagging these plainly, as requested, rather than burying them:

1. **`schema_validator.py`'s archive/lifecycle companion nudge (rules 3/4) is
   the single biggest tension this research surfaces.** A large fraction of
   tonight's work (the `SCHEMA_REFINE_MIN_SCORE` change, the
   `_COMPANION_ARCHIVE_RE`/`_COMPANION_LIFECYCLE_RE` attribution routing) was
   specifically about making the refiner chase this nudge *more* aggressively
   across every non-exempt header table. This research found no enterprise
   consensus supporting that blanket mandate — the best sources (Microsoft's
   own temporal-table usage docs, Fowler on event sourcing) are explicitly
   scenario-based. **If this check is rescoped to fire only for
   financial/regulated/high-criticality tables** (per 2.1), a meaningful
   fraction of the score-90 convergence difficulty seen across tonight's
   showcase runs would likely disappear on its own, independent of the
   FK-referential-action reliability problem that's driving most current
   rejections. This is worth deciding on explicitly before more time is spent
   chasing convergence against the current (broader) version of this rule.
2. **The current FK-referential-action check's single "always suggest
   CASCADE" default is shown by this research to be actively wrong in two
   specific cases**: soft-delete parent tables (CASCADE there is a false
   sense of safety, not a fix) and independent-entity relationships (Postgres's
   own docs say RESTRICT is correct there, not CASCADE). The logic upgrade in
   2.1 is a real behavior change to the suggestion text the refiner currently
   feeds the LLM, not just a docstring edit.
3. **The completeness gate (`SCHEMA_COMPLETENESS_MIN_RATIO`) and the entire
   `run_showcase.py` shippability logic assume exactly one schema.**
   Decomposition would need a parallel per-schema-module completeness
   definition — this is explicitly out of scope until 2.2 is approved and
   built, flagged here only so it isn't discovered as a surprise later.
4. **Tonight's `_MAX_TARGET_TABLES`/iteration-budget tuning was entirely about
   getting one 40-60 table schema to converge reliably** — that is an LLM
   fix-reliability problem, and this research doesn't resolve it directly.
   Auto-decomposition would only help if a given business domain genuinely
   contains multiple loosely-coupled bounded contexts (2.2's coupling-strength
   test) — most of the current showcase briefs (a financial ledger's core
   double-entry accounting, for instance) are arguably one bounded context
   each, so decomposition is not a shortcut around the current convergence
   problem for those specific domains. It's a separate capability for
   genuinely multi-domain projects, not a fix for the current showcase set.

---

## PART 3 — Decisions needed before any code changes

1. **Approve/reject the threshold-logic changes in 2.1** — these can land
   independently of everything else and are the most immediately actionable.
2. **Decide on the archive/lifecycle companion rescoping (2.5, item 1)**
   specifically, since it's both the highest-impact single change and directly
   affects work already in flight this session.
3. **Approve/reject building schema-decomposition (2.2-2.4) at all right
   now**, given it's flagged as lower-priority relative to single-schema
   reliability, and given the real refactor cost to `schema_refiner.py`
   specifically.
4. If decomposition is approved, **decide on the trigger mechanism** in 2.2
   step 2 (explicit user-confirmation vs. some other signal) before any
   pipeline prompt changes are written.

No code has been changed. Awaiting direction on the above before implementing
anything in Part 2.
