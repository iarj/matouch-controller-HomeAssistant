"""Home Assistant diagnostic UI for MaTouch runtime mappings."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_ENTITY_ID,
    EntityCategory,
    Platform,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from . import MatouchControllerData
from .active_mask import ActiveMaskObservation
from .const import (
    CONF_CONTROL_SLOT,
    CONF_DEVICE_UID,
    CONF_DISPLAY_NAME,
    CONF_LABEL,
    CONF_PAGE_SLOT,
    DEFAULT_DISPLAY_NAME,
    DOMAIN,
    page_device_identifier,
)
from .manifest_model import ControlManifest, PageManifest, RuntimeManifest
from .mapping import effective_entry_data, mapping_for_slots
from .sync import EntryDisplaySync, scene_launcher_availability


def _display_name(entry: ConfigEntry, effective: Mapping[str, Any]) -> str:
    """Return the human-facing parent device name."""
    configured = effective.get(CONF_DISPLAY_NAME)
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    if isinstance(entry.title, str) and entry.title.strip():
        return entry.title.strip()
    return DEFAULT_DISPLAY_NAME


def _control_mapping_unique_id(
    device_uid: str,
    page_slot: int,
    control_slot: int,
) -> str:
    """Return the stable diagnostic identity for one compiled control."""
    return f"{device_uid}_page_{page_slot}_control_{control_slot}_mapping"


def _is_control_mapping_unique_id(device_uid: str, unique_id: str) -> bool:
    """Return whether a unique ID belongs to this device's control diagnostics."""
    prefix = f"{device_uid}_page_"
    suffix = "_mapping"
    if not unique_id.startswith(prefix) or not unique_id.endswith(suffix):
        return False

    address = unique_id[len(prefix) : -len(suffix)]
    page_slot, separator, control_slot = address.partition("_control_")
    return bool(
        separator
        and page_slot.isdigit()
        and control_slot.isdigit()
    )


def _remove_stale_control_mapping_registry_entries(
    hass: HomeAssistant,
    entry: ConfigEntry,
    manifest: RuntimeManifest,
) -> None:
    """Remove diagnostics whose compiled control disappeared from the manifest."""
    valid_unique_ids = {
        _control_mapping_unique_id(
            manifest.device_uid,
            page.page_slot,
            control.control_slot,
        )
        for page in manifest.pages
        for control in page.controls
    }
    entity_registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(
        entity_registry,
        entry.entry_id,
    ):
        unique_id = registry_entry.unique_id
        if (
            registry_entry.domain != Platform.SENSOR
            or registry_entry.platform != DOMAIN
            or not _is_control_mapping_unique_id(manifest.device_uid, unique_id)
            or unique_id in valid_unique_ids
        ):
            continue
        entity_registry.async_remove(registry_entry.entity_id)


def _remove_stale_page_devices(
    registry: dr.DeviceRegistry,
    entry: ConfigEntry,
    manifest: RuntimeManifest,
    parent_device_id: str,
) -> None:
    """Remove this panel's page children absent from its compiled manifest."""
    compiled_slots = {page.page_slot for page in manifest.pages}
    # The protocol reserves six functional slots. Resolve exact owned page
    # identities; unrelated children and other panels must remain untouched.
    for page_slot in range(1, 7):
        if page_slot in compiled_slots:
            continue
        child = registry.async_get_child_device_by_identifier(
            (DOMAIN, page_device_identifier(manifest.device_uid, page_slot)),
            entry.entry_id,
        )
        if child is not None and child.parent_device_id == parent_device_id:
            registry.async_remove_device(child.id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Expose one parent MaTouch and one child device per functional page."""
    effective = effective_entry_data(entry)
    device_uid = effective.get(CONF_DEVICE_UID)
    data = hass.data.get(DOMAIN)
    if (
        not isinstance(device_uid, str)
        or not device_uid
        or not isinstance(data, MatouchControllerData)
    ):
        return

    manifest = data.manifests.get(device_uid)
    sync = data.syncs.get(device_uid)
    if manifest is None or sync is None:
        return

    _remove_stale_control_mapping_registry_entries(hass, entry, manifest)

    device_registry = dr.async_get(hass)
    name = _display_name(entry, effective)
    parent = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, device_uid)},
        name=name,
        model=manifest.product_profile,
        sw_version=manifest.firmware_version,
    )

    _remove_stale_page_devices(device_registry, entry, manifest, parent.id)

    for page in manifest.pages:
        device_registry.async_get_or_create_child(
            config_entry_id=entry.entry_id,
            parent_device_id=parent.id,
            identifiers={(DOMAIN, page_device_identifier(device_uid, page.page_slot))},
            name=page.title,
        )

    entities: list[SensorEntity] = [
        MaTouchRuntimeConfigurationSensor(
            manifest=manifest,
            sync=sync,
            syncs=data.syncs,
            active_masks=data.active_masks,
            parent_name=name,
        )
    ]
    for page in manifest.pages:
        for control in page.controls:
            mapping = mapping_for_slots(
                sync.mappings,
                page.page_slot,
                control.control_slot,
            )
            entities.append(
                MaTouchControlMappingSensor(
                    hass=hass,
                    manifest=manifest,
                    page=page,
                    control=control,
                    mapping=mapping,
                    parent_device_id=parent.id,
                )
            )

    async_add_entities(entities)


class MaTouchRuntimeConfigurationSensor(SensorEntity):
    """Read-only configuration visibility for one physical MaTouch."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_name = "Runtime configuration"
    _attr_should_poll = True

    def __init__(
        self,
        *,
        manifest: RuntimeManifest,
        sync: EntryDisplaySync,
        active_masks: Mapping[str, ActiveMaskObservation],
        parent_name: str,
        syncs: Mapping[str, EntryDisplaySync] | None = None,
    ) -> None:
        self._manifest = manifest
        self._sync = sync
        self._syncs = syncs
        self._active_masks = active_masks
        self._attr_unique_id = f"{manifest.device_uid}_runtime_configuration"
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, manifest.device_uid)},
            name=parent_name,
            model=manifest.product_profile,
            sw_version=manifest.firmware_version,
        )

    @property
    def _current_sync(self) -> EntryDisplaySync | None:
        if self._syncs is None:
            return self._sync
        return self._syncs.get(self._manifest.device_uid)

    @property
    def available(self) -> bool:
        sync = self._current_sync
        return sync is not None and not sync._stopped

    @property
    def native_value(self) -> int | None:
        """Expose the persistent runtime config revision."""
        sync = self._current_sync
        return None if sync is None else sync.config_revision

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose useful integration/runtime metadata without duplicating controls."""
        active_mask = self._active_masks.get(self._manifest.device_uid)
        sync = self._current_sync
        return {
            "device_uid": self._manifest.device_uid,
            "firmware_version": self._manifest.firmware_version,
            "protocol_version": self._manifest.protocol_version,
            "schema_version": self._manifest.schema_version,
            "template_library_version": self._manifest.template_library_version,
            "config_hash": self._manifest.config_hash,
            "integration_session_id": None if sync is None else sync.integration_session_id,
            "active_boot_id": None if sync is None else sync.active_boot_id,
            "functional_pages": len(self._manifest.pages),
            "mapped_controls": None if sync is None else len(sync.mappings),
            "compiled_mask": (
                None if active_mask is None else active_mask.compiled_mask
            ),
            "active_mask": (
                None if active_mask is None else active_mask.active_mask
            ),
            "active_mask_stale": (
                True if active_mask is None else active_mask.stale
            ),
        }


class MaTouchControlMappingSensor(SensorEntity):
    """Read-only visibility for one compiled MaTouch control."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        *,
        hass: HomeAssistant,
        manifest: RuntimeManifest,
        page: PageManifest,
        control: ControlManifest,
        mapping: Mapping[str, Any] | None,
        parent_device_id: str,
    ) -> None:
        self._hass = hass
        self._manifest = manifest
        self._page = page
        self._control = control
        self._mapping = None if mapping is None else dict(mapping)
        self._entity_id = (
            None if self._mapping is None else self._mapping[CONF_ENTITY_ID]
        )
        mapping_label = (
            None
            if self._mapping is None
            else self._mapping.get(CONF_LABEL)
        )
        self._attr_name = (
            mapping_label
            if isinstance(mapping_label, str)
            else f"Control {control.control_slot}"
        )
        self._attr_icon = control.icon
        self._attr_unique_id = _control_mapping_unique_id(
            manifest.device_uid,
            page.page_slot,
            control.control_slot,
        )
        self._attr_device_info = dr.ChildDeviceInfo(
            identifiers={
                (DOMAIN, page_device_identifier(manifest.device_uid, page.page_slot))
            },
            name=page.title,
            parent_device_id=parent_device_id,
        )

    def _mapped_state(self):
        if self._entity_id is None:
            return None
        return self._hass.states.get(self._entity_id)

    def _mapping_status(self) -> str:
        """Return mapping/availability status for this compiled control."""
        if self._entity_id is None:
            return "unmapped"
        state = self._mapped_state()
        if state is None:
            return "missing"
        if self._page.template == "scene_launcher":
            available, _ = scene_launcher_availability(self._entity_id, state)
            if available:
                return "available"
            return "unknown" if state.state == STATE_UNKNOWN else "unavailable"
        if state.state == STATE_UNAVAILABLE:
            return "unavailable"
        if state.state == STATE_UNKNOWN:
            return "unknown"
        return "available"

    @property
    def native_value(self) -> str:
        """Expose the mapped HA entity or explicit unmapped state."""
        return "unmapped" if self._entity_id is None else self._entity_id

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the control contract and its authoritative HA mapping."""
        state = self._mapped_state()
        available = (
            self._entity_id is not None
            and state is not None
            and state.state not in {STATE_UNAVAILABLE, STATE_UNKNOWN}
        )
        if self._page.template == "scene_launcher" and self._entity_id is not None:
            available, _ = scene_launcher_availability(self._entity_id, state)
        return {
            "page_slot": self._page.page_slot,
            "control_slot": self._control.control_slot,
            "label": (
                None if self._mapping is None else self._mapping.get(CONF_LABEL)
            ),
            "function": self._control.function,
            "state_kind": self._control.state_kind,
            "mapped_entity": self._entity_id,
            "mapping_status": self._mapping_status(),
            "mapped_entity_available": available,
            "mapped_entity_state": None if state is None else state.state,
        }

    async def async_added_to_hass(self) -> None:
        """Refresh diagnostic visibility when the mapped entity changes."""
        await super().async_added_to_hass()
        if self._entity_id is None:
            return
        self.async_on_remove(
            async_track_state_change_event(
                self._hass,
                [self._entity_id],
                self._mapped_entity_changed,
            )
        )

    @callback
    def _mapped_entity_changed(self, event: Event) -> None:
        """Write the latest mapped availability/state to Home Assistant."""
        self.async_write_ha_state()

