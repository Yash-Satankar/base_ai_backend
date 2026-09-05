"""
Tests for the schema-decomposition trigger detector
(app/engine/decomposition_signals.py) — see
docs/enterprise_standards_spec.md §2.2. Deliberately conservative and
deterministic: no source in the research supports inferring a schema split
from anything other than an explicit organizational/product signal.
"""

import pytest

from app.engine.decomposition_signals import detect_decomposition_signal


@pytest.mark.parametrize("text", [
    "We have separate schemas for billing and clinical teams.",
    "This will eventually need its own database per product.",
    "The billing team and the clinical team need to operate independently.",
    "We're building this as microservices from day one.",
    "Different departments will each own their own part of this.",
    "Please split this into services for orders and inventory.",
    "Each team owns their own tables and deploys independently.",
])
def test_explicit_organizational_signals_detected(text):
    assert detect_decomposition_signal(text) is True


@pytest.mark.parametrize("text", [
    "We run a logistics company with 50 branches.",
    "I need a healthcare system for a 300-bed hospital.",
    "This is a complex system with many tables and modules.",
    "We have a large, enterprise-scale multi-tenant SaaS product.",
    "",
    None,
])
def test_ordinary_requirements_not_flagged(text):
    assert detect_decomposition_signal(text) is False
