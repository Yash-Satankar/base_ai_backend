"""
The generation prompts must carry the production-hardening mandates that
rules 21 / 22 / 23 encode, and must not carry stale corpus-size claims.
"""

import re

from app.prompts.system_prompt import (
    build_system_prompt,
    build_module_prompt,
    build_stitch_prompt,
)
from app.validators.schema_validator import rule_count

STUB_RULES = [
    {
        "rule_id": 21,
        "rule_name": "Storage Engine: InnoDB Mandatory",
        "priority": "critical",
        "enforce": ["every table must be ENGINE=InnoDB"],
        "avoid": ["ENGINE=MyISAM"],
    },
]


def test_system_prompt_mandates_innodb_utf8mb4_datetime():
    p = build_system_prompt(STUB_RULES)
    assert "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci" in p
    assert "MyISAM" in p
    assert re.search(r"latin1.*utf8mb3|utf8mb3.*latin1|latin1/utf8/utf8mb3", p)
    assert "DATE" in p and "DATETIME" in p


def test_system_prompt_has_no_stale_corpus_numbers():
    p = build_system_prompt(STUB_RULES)
    assert "109 proprietary architecture rules" not in p
    assert "23 production MySQL databases" not in p
    # live rule count is injected
    assert f"{rule_count()} proprietary architecture rules" in p


def test_unique_id_header_all_example_has_engine_and_charset():
    p = build_system_prompt(STUB_RULES)
    block = p.split("unique_id_header_all", 1)[1]
    head = block[: block.find(";") + 1]
    assert "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4" in head


def test_module_prompt_checklist_covers_engine_charset_and_datetime():
    module = {
        "name": "Core",
        "description": "core entities",
        "tables": [{"name": "widget_header_all", "purpose": "widget master"}],
    }
    p = build_module_prompt(module, "general", gst_required=False, scale="medium")
    assert "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci" in p
    assert "DATETIME" in p and "never DATE" in p


def test_stitch_prompt_lists_engine_charset_and_date_fixes():
    p = build_stitch_prompt([{"sql": "CREATE TABLE a_all (id INT);", "name": "m"}], "Proj")
    assert "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci" in p
    assert "typed DATE" in p
