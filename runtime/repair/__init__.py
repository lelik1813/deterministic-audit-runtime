"""
Deterministic Repair Layer (STEP 5)

This module provides deterministic repair functionality for typed IR,
applying only classified repairs and logging all modifications.

Core Principle:
    Deterministic repair may ONLY modify fields that are both defaultable AND non-semantic.
    All other modifications require model retry with failure mode classification.

From: deterministic_repair_design.md
Step: STEP 5 — Deterministic Repair Layer Design
"""

from runtime.repair.types import (
    RepairType,
    RepairLogEntry,
    RepairLog,
    RepairContext,
    RepairedTypedIR,
    RepairRequiredError,
)
from runtime.repair.repairer import DeterministicRepairer, create_repair_context
from runtime.repair.entity_type_mapping import (
    EVENT_TYPE_TO_ENTITY_TYPE,
    derive_entity_type,
)
from runtime.repair.status_derivation import (
    EVENT_TYPE_TO_STATUS,
    derive_status,
    get_status_derivation_map,
)

__all__ = [
    # Types
    "RepairType",
    "RepairLogEntry",
    "RepairLog",
    "RepairContext",
    "RepairedTypedIR",
    "RepairRequiredError",
    # Repairer
    "DeterministicRepairer",
    "create_repair_context",
    # Entity Type Mapping
    "EVENT_TYPE_TO_ENTITY_TYPE",
    "derive_entity_type",
    # Status Derivation
    "EVENT_TYPE_TO_STATUS",
    "derive_status",
    "get_status_derivation_map",
]
