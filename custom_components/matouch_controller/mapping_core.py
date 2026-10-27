"""Generic persistent mapping primitives without template policy."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import unicodedata

from homeassistant.const import CONF_ENTITY_ID

from .const import (
    CONF_CONTROL_SLOT,
    CONF_LABEL,
    CONF_PAGE_SLOT,
    RUNTIME_LABEL_MAX_LENGTH,
    RUNTIME_LABEL_REQUIRED,
)
from .manifest_model import ControlManifest, RuntimeManifest


_RUNTIME_LABEL_LATIN_V1_EXTRA_CHARS = frozenset("ĂăÂâÎîȘșȚțŞşŢţ")


class MappingError(ValueError):
    """Raised when a persistent runtime mapping is invalid."""


class RuntimeLabelError(MappingError):
    """Raised for one user-correctable runtime label validation error."""

    def __init__(self, translation_key: str, message: str) -> None:
        super().__init__(message)
        self.translation_key = translation_key


def _runtime_label_character_allowed(character: str) -> bool:
    """Return whether one character belongs to Runtime Label Latin v1."""
    codepoint = ord(character)
    return (
        0x20 <= codepoint <= 0x7E
        or character in _RUNTIME_LABEL_LATIN_V1_EXTRA_CHARS
    )


def validate_runtime_label(value: Any) -> str:
    """Normalize and validate the fixed global Runtime Label Latin v1 policy."""
    if not isinstance(value, str):
        raise RuntimeLabelError(
            "label_required",
            "Runtime label must contain non-whitespace text",
        )

    normalized = unicodedata.normalize("NFC", value)

    if RUNTIME_LABEL_REQUIRED and not normalized.strip():
        raise RuntimeLabelError(
            "label_required",
            "Runtime label must contain non-whitespace text",
        )

    if any(
        not _runtime_label_character_allowed(character)
        for character in normalized
    ):
        raise RuntimeLabelError(
            "label_unsupported_chars",
            "Runtime label contains characters outside Runtime Label Latin v1",
        )

    if len(normalized) > RUNTIME_LABEL_MAX_LENGTH:
        raise RuntimeLabelError(
            "label_too_long",
            f"Runtime label exceeds maximum length {RUNTIME_LABEL_MAX_LENGTH}",
        )

    return normalized


def mapping_record(
    control: ControlManifest,
    entity_id: str,
    label: str | None = None,
) -> dict[str, Any]:
    """Return one canonical persistent mapping record."""
    mapping = {
        CONF_PAGE_SLOT: control.page_slot,
        CONF_CONTROL_SLOT: control.control_slot,
        CONF_ENTITY_ID: entity_id,
    }
    if label is not None:
        mapping[CONF_LABEL] = label
    return mapping


def mapping_for_slots(
    mappings: Any,
    page_slot: int,
    control_slot: int,
) -> Mapping[str, Any] | None:
    """Return the persistent mapping for one canonical slot address."""
    if not isinstance(mappings, list):
        return None

    matches = [
        mapping
        for mapping in mappings
        if isinstance(mapping, Mapping)
        and mapping.get(CONF_PAGE_SLOT) == page_slot
        and mapping.get(CONF_CONTROL_SLOT) == control_slot
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def control_for_slots(
    manifest: RuntimeManifest,
    page_slot: int,
    control_slot: int,
) -> ControlManifest | None:
    """Return the manifest capability at one canonical slot address."""
    matches = [
        control
        for control in manifest.controls
        if control.page_slot == page_slot
        and control.control_slot == control_slot
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def _mapping_slot_address(raw: Mapping[str, Any]) -> tuple[int, int]:
    """Validate and return one canonical persistent mapping address."""
    page_slot = raw.get(CONF_PAGE_SLOT)
    control_slot = raw.get(CONF_CONTROL_SLOT)
    if (
        isinstance(page_slot, bool)
        or not isinstance(page_slot, int)
        or not 1 <= page_slot <= 6
        or isinstance(control_slot, bool)
        or not isinstance(control_slot, int)
        or control_slot < 1
    ):
        raise MappingError("Invalid mapping slot address")
    return page_slot, control_slot


def _normalize_mapping_for_control(
    raw: Mapping[str, Any],
    control: ControlManifest,
) -> dict[str, Any]:
    """Validate one mapping against one active manifest control."""
    entity_id = raw.get(CONF_ENTITY_ID)
    if not isinstance(entity_id, str) or "." not in entity_id:
        raise MappingError("Invalid mapped entity_id")

    domain = entity_id.split(".", 1)[0]
    if domain not in control.allowed_domains:
        raise MappingError("Mapped entity domain is not allowed")

    label = (
        validate_runtime_label(raw.get(CONF_LABEL))
        if CONF_LABEL in raw
        else None
    )
    return mapping_record(control, entity_id, label)


def validate_generic_mappings(
    manifest: RuntimeManifest,
    mappings: Any,
) -> list[dict[str, Any]]:
    """Validate generic mapping structure without template-specific atomicity."""
    if not isinstance(mappings, list):
        raise MappingError("Mappings must be a list")

    normalized: list[dict[str, Any]] = []
    seen_addresses: set[tuple[int, int]] = set()
    seen_entities_by_page: dict[int, set[str]] = {}

    for raw in mappings:
        if not isinstance(raw, Mapping):
            raise MappingError("Each mapping must be an object")

        page_slot, control_slot = _mapping_slot_address(raw)
        address = (page_slot, control_slot)
        if address in seen_addresses:
            raise MappingError("Duplicate mapping slot address")

        control = control_for_slots(manifest, page_slot, control_slot)
        if control is None:
            raise MappingError("Mapping address is not present in runtime manifest")

        mapping = _normalize_mapping_for_control(raw, control)
        entity_id = mapping[CONF_ENTITY_ID]
        page_entities = seen_entities_by_page.setdefault(page_slot, set())
        if entity_id in page_entities:
            raise MappingError("Duplicate entity mapping within one page")

        seen_addresses.add(address)
        page_entities.add(entity_id)
        normalized.append(mapping)

    return normalized


def revalidate_generic_mappings(
    manifest: RuntimeManifest,
    mappings: Any,
) -> list[dict[str, Any]]:
    """Preserve generically valid mappings after a compiled config change."""
    if not isinstance(mappings, list):
        raise MappingError("Mappings must be a list")

    normalized: list[dict[str, Any]] = []
    seen_addresses: set[tuple[int, int]] = set()
    seen_entities_by_page: dict[int, set[str]] = {}

    for raw in mappings:
        if not isinstance(raw, Mapping):
            raise MappingError("Each mapping must be an object")

        page_slot, control_slot = _mapping_slot_address(raw)
        address = (page_slot, control_slot)
        if address in seen_addresses:
            raise MappingError("Duplicate mapping slot address")
        seen_addresses.add(address)

        control = control_for_slots(manifest, page_slot, control_slot)
        if control is None:
            continue

        entity_id = raw.get(CONF_ENTITY_ID)
        if not isinstance(entity_id, str) or "." not in entity_id:
            raise MappingError("Invalid mapped entity_id")

        domain = entity_id.split(".", 1)[0]
        if domain not in control.allowed_domains:
            continue

        label = (
            validate_runtime_label(raw.get(CONF_LABEL))
            if CONF_LABEL in raw
            else None
        )
        page_entities = seen_entities_by_page.setdefault(page_slot, set())
        if entity_id in page_entities:
            raise MappingError("Duplicate entity mapping within one page")

        page_entities.add(entity_id)
        normalized.append(mapping_record(control, entity_id, label))

    return validate_generic_mappings(manifest, normalized)
