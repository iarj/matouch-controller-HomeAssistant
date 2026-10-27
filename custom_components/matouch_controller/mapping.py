"""Persistent MaTouch mapping facade and persistence helpers."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_MANIFEST_ACTION,
    CONF_MAPPINGS,
    CONF_VALIDATED_CONFIG_HASH,
)
from .mapping_core import (
    MappingError,
    RuntimeLabelError,
    control_for_slots,
    mapping_for_slots,
    mapping_record,
    validate_runtime_label,
)
from .runtime_config import (
    revalidate_persistent_mappings,
    validate_persistent_mappings,
)


_LEGACY_CAPABILITIES_KEY = "capabilities"

__all__ = [
    "MappingError",
    "RuntimeLabelError",
    "control_for_slots",
    "effective_entry_data",
    "mapping_for_slots",
    "mapping_record",
    "persist_manifest_action",
    "persist_runtime_validation",
    "revalidate_persistent_mappings",
    "validate_persistent_mappings",
    "validate_runtime_label",
]


def persist_runtime_validation(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    config_hash: str,
    mappings: list[dict[str, Any]],
) -> None:
    """Persist validated hash and normalized mappings without changing revision."""
    data = dict(entry.data)
    options = dict(entry.options)
    changed = False

    if data.pop(_LEGACY_CAPABILITIES_KEY, None) is not None:
        changed = True
    if options.pop(_LEGACY_CAPABILITIES_KEY, None) is not None:
        changed = True

    if CONF_MAPPINGS in entry.options:
        if options.get(CONF_VALIDATED_CONFIG_HASH) != config_hash:
            options[CONF_VALIDATED_CONFIG_HASH] = config_hash
            changed = True
        if options.get(CONF_MAPPINGS) != mappings:
            options[CONF_MAPPINGS] = mappings
            changed = True
    else:
        if data.get(CONF_VALIDATED_CONFIG_HASH) != config_hash:
            data[CONF_VALIDATED_CONFIG_HASH] = config_hash
            changed = True
        if data.get(CONF_MAPPINGS) != mappings:
            data[CONF_MAPPINGS] = mappings
            changed = True

    if changed:
        hass.config_entries.async_update_entry(
            entry,
            data=data,
            options=options,
        )


def persist_manifest_action(
    hass: HomeAssistant,
    entry: ConfigEntry,
    manifest_action: str,
) -> None:
    """Atomically replace only the persisted manifest action (HA §9.8 pt. 4).

    The action is written where it is effective (options override data), so
    device_uid, display_name, mappings, config_revision and
    validated_config_hash are left untouched.
    """
    if CONF_MANIFEST_ACTION in entry.options:
        options = dict(entry.options)
        options[CONF_MANIFEST_ACTION] = manifest_action
        hass.config_entries.async_update_entry(entry, options=options)
        return

    data = dict(entry.data)
    data[CONF_MANIFEST_ACTION] = manifest_action
    hass.config_entries.async_update_entry(entry, data=data)


def effective_entry_data(entry: ConfigEntry) -> dict[str, Any]:
    """Return effective persistent runtime configuration for an entry."""
    data = dict(entry.data)
    data.update(entry.options)
    return data
