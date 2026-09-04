"""
Tests for the auto-iteration refinement loop
(``app/services/schema_refiner.py``).

* convergence + honest non-convergence use a **real MySQL 8** (the refiner runs
  the execution validator every iteration) — skipped when no backend, unless
  ``REQUIRE_MYSQL_EXEC_TESTS=1``.
* cost-attribution + Decision-B degrade are structural-only (no MySQL needed)
  and always run.
"""

import pytest

from app.core.config import settings
from app.conversation import llm_client
from app.services import schema_refiner as sr
from app.services.schema_refiner import refine_until_clean


# ───────────────────────── convergence (real MySQL) ─────────────────────────

# MyISAM + latin1, no audit timestamps, no id registry — broken structurally
# and at the engine.
_BROKEN_SEED = """
CREATE TABLE vendor_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(120) NOT NULL,
  status INT NOT NULL DEFAULT 1
) ENGINE=MyISAM DEFAULT CHARSET=latin1;
"""

# still wrong after "iteration 1": engine fixed, charset still latin1 (a
# structural *high*), still no id registry.
_HALF_FIXED = """
CREATE TABLE vendor_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(120) NOT NULL,
  status INT NOT NULL DEFAULT 1 COMMENT '1=active,2=inactive',
  created_on DATETIME NOT NULL,
  modified_on DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=latin1;
"""

_CLEAN = """
CREATE TABLE unique_id_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  table_name VARCHAR(100) NOT NULL,
  prefix VARCHAR(20) NOT NULL,
  last_id VARCHAR(15) NOT NULL DEFAULT '00000',
  status INT NOT NULL DEFAULT 1 COMMENT '1=active,2=inactive',
  created_on DATETIME NOT NULL,
  modified_on DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE vendor_header_all (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(120) NOT NULL,
  status INT NOT NULL DEFAULT 1 COMMENT '1=active,2=inactive',
  created_on DATETIME NOT NULL,
  modified_on DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


@pytest.fixture
def scripted_llm(monkeypatch):
    """Replace the LLM with a scripted sequence of responses (consumed in
    order; the last one repeats). Records every call."""
    calls = []

    def _make(responses):
        seq = list(responses)

        def fake_call_llm(*, operation, system_prompt, user_prompt, session_id=None,
                          project_id=None, max_tokens=None, model=None,
                          temperature=None, cache=False, degrade=False):
            calls.append({"operation": operation, "session_id": session_id,
                          "degrade": degrade, "user_prompt": user_prompt})
            content = seq[len(calls) - 1] if len(calls) <= len(seq) else seq[-1]
            return {
                "content": content,
                "provider": "groq",
                "model": settings.DEGRADE_MODEL if degrade else settings.GROQ_MODEL,
                "usage": {"input_tokens": 3000, "output_tokens": 1200},
                "operation": operation,
                "cost_usd": 0.0021,
                "degraded": degrade,
                "cached": False,
            }

        monkeypatch.setattr(sr.llm_client, "call_llm", fake_call_llm)
        return calls

    return _make


def test_broken_schema_converges_within_max_iterations(mysql_enabled, scripted_llm):
    calls = scripted_llm([_HALF_FIXED, _CLEAN])

    result = refine_until_clean(
        _BROKEN_SEED,
        {"requirement": "vendor registry", "system_prompt": "SYS", "session_id": "conv-refine-1"},
        max_iterations=3,
    )

    assert result.converged is True
    assert result.iterations_used == 2
    assert result.iterations_used <= 3
    assert result.remaining_issues == []
    assert "unique_id_header_all" in result.final_ddl
    # it actually called the model twice, tagged correctly
    assert [c["operation"] for c in calls] == ["schema_refine", "schema_refine"]
    # the fix prompt carried concrete findings, not a vague ask
    first_prompt = calls[0]["user_prompt"]
    assert "MyISAM" in first_prompt or "non-innodb" in first_prompt
    assert "improve this" not in first_prompt.lower()


def test_unfixable_schema_stops_at_cap_and_reports_honestly(mysql_enabled, scripted_llm):
    # every "fix" reintroduces a FK type mismatch MySQL will reject — never clean.
    def _broken(n):
        return f"""
        CREATE TABLE p_all (id INT PRIMARY KEY, created_on DATETIME NOT NULL,
          modified_on DATETIME NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        CREATE TABLE c_all (
          id INT PRIMARY KEY,
          p_ref VARCHAR(20) NOT NULL,   -- attempt {n}
          created_on DATETIME NOT NULL, modified_on DATETIME NOT NULL,
          CONSTRAINT fk_c_p FOREIGN KEY (p_ref) REFERENCES p_all (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """

    calls = scripted_llm([_broken(1), _broken(2), _broken(3), _broken(4)])

    result = refine_until_clean(
        _broken(0),
        {"requirement": "x", "system_prompt": "SYS", "session_id": "conv-refine-2"},
        max_iterations=3,
    )

    assert result.converged is False
    assert result.iterations_used == 3          # ran to the cap
    assert len(calls) == 3
    assert result.remaining_issues, "must list what is still wrong"
    assert any(iss["source"] == "mysql" and "incompatible" in iss["message"].lower()
               for iss in result.remaining_issues)
    # honest, not silently 'done'
    assert "did NOT converge" in result.summary()


# ─────────────────── cost attribution (structural only) ───────────────────

@pytest.fixture
def structural_only(monkeypatch):
    monkeypatch.setattr(settings, "MYSQL_EXEC_VALIDATION_ENABLED", False)


_NEVER_CLEAN_SEED = """
CREATE TABLE widgets (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(100) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


@pytest.fixture
def stub_generate_schema(monkeypatch):
    """Stub the actual model call inside the real llm_client so cost accounting,
    operation tagging and degrade routing all run for real."""
    ops = []
    n = {"i": 0}

    def fake_generate_schema(system_prompt, user_prompt, max_tokens=None, model=None, temperature=None):
        # still structurally broken (widgets: bad name, no id registry, no
        # timestamps) but a different string each call so the loop doesn't
        # early-exit on "no change"
        n["i"] += 1
        return {
            "content": _NEVER_CLEAN_SEED.replace("VARCHAR(100)", f"VARCHAR({100 + n['i']})"),
            "provider": "groq",
            "model": model or settings.GROQ_MODEL,
            "usage": {"input_tokens": 2500, "output_tokens": 900},
        }

    real_log = llm_client.TelemetryManager.log_operation

    def spy_log(**kw):
        ops.append(kw.get("operation"))
        return real_log(**kw)

    monkeypatch.setattr(llm_client, "generate_schema", fake_generate_schema)
    monkeypatch.setattr(llm_client.TelemetryManager, "log_operation", spy_log)
    return ops


def test_cost_is_attributed_to_the_conversation_per_iteration(structural_only, stub_generate_schema):
    sid = "conv-refine-cost"
    assert llm_client.conversation_cost(sid) == 0.0

    result = refine_until_clean(
        _NEVER_CLEAN_SEED,
        {"requirement": "widgets", "system_prompt": "SYS", "session_id": sid},
        max_iterations=3,
    )

    assert result.iterations_used == 3
    # every iteration was tagged and priced
    assert stub_generate_schema == ["schema_refine", "schema_refine", "schema_refine"]
    refine_records = [h for h in result.history if h["phase"] == "refine"]
    assert len(refine_records) == 3
    assert all(r["cost_usd"] > 0 for r in refine_records)
    assert result.total_cost_usd == pytest.approx(sum(r["cost_usd"] for r in refine_records))
    # and it landed on the conversation's running cost, like every other call
    assert llm_client.conversation_cost(sid) == pytest.approx(result.total_cost_usd, rel=1e-3)


# ─────────────────── Decision-B degrade (structural only) ───────────────────

def test_degraded_conversation_is_capped_to_one_iteration(structural_only, stub_generate_schema, monkeypatch):
    monkeypatch.setattr(settings, "CONVERSATION_COST_WARN_USD", 0.001)
    sid = "conv-refine-degraded"
    llm_client._add_conversation_cost(sid, 0.01)          # push it over the soft ceiling
    assert llm_client.should_degrade(sid) is True

    result = refine_until_clean(
        _NEVER_CLEAN_SEED,
        {"requirement": "widgets", "system_prompt": "SYS", "session_id": sid},
        max_iterations=3,                                  # would be 3 if not degraded
    )

    assert result.degraded is True
    assert result.iterations_used == 1
    assert len([h for h in result.history if h["phase"] == "refine"]) == 1
    assert stub_generate_schema == ["schema_refine"]      # exactly one model call
    assert result.converged is False
    assert result.remaining_issues


def test_explicit_degraded_flag_overrides_auto_detection(structural_only, scripted_llm):
    calls = scripted_llm([
        _NEVER_CLEAN_SEED.replace("VARCHAR(100)", "VARCHAR(101)"),
        _NEVER_CLEAN_SEED.replace("VARCHAR(100)", "VARCHAR(102)"),
        _NEVER_CLEAN_SEED.replace("VARCHAR(100)", "VARCHAR(103)"),
    ])
    result = refine_until_clean(
        _NEVER_CLEAN_SEED,
        {"requirement": "widgets", "system_prompt": "SYS", "session_id": "conv-x"},
        max_iterations=3,
        degraded=True,
    )
    assert result.degraded is True
    assert result.iterations_used == 1
    assert len(calls) == 1


# ─────────────────── shrink guard (structural only) ───────────────────

_MULTI_TABLE_SEED = "\n".join(
    f"CREATE TABLE t{i}_all (id INT AUTO_INCREMENT PRIMARY KEY, "
    f"name VARCHAR(100) NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
    for i in range(12)
)  # 12 tables, structurally broken (no timestamps / id registry / suffix rules)


def test_refiner_discards_iteration_that_drops_tables(structural_only, scripted_llm):
    """A whole-schema rewrite that comes back with far fewer tables (truncated
    response) is rejected — final_ddl keeps the pre-refine schema, not the
    shrunk one."""
    shrunk = "\n".join(
        f"CREATE TABLE t{i}_all (id INT PRIMARY KEY, created_on DATETIME NOT NULL, "
        f"modified_on DATETIME NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
        for i in range(4)
    )  # only 4 of the 12 tables
    calls = scripted_llm([shrunk, shrunk, shrunk])

    result = refine_until_clean(
        _MULTI_TABLE_SEED,
        {"requirement": "x", "system_prompt": "SYS", "session_id": "conv-shrink"},
        max_iterations=3,
    )

    from app.services.schema_refiner import _table_names
    assert len(_table_names(result.final_ddl)) == 12          # kept the full set
    assert len(calls) == 1                                    # stopped after the bad revision
    assert result.converged is False
    assert any(h.get("rejected_shrunk") for h in result.history)
