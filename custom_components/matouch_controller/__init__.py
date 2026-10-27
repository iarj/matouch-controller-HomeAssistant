"""MaTouch Controller integration."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import (
    ConfigEntryError,
    ConfigEntryNotReady,
    ServiceValidationError,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .active_mask import (
    ActiveMaskObservation,
    ActiveMaskTransportResponseError,
    ActiveMaskTransportUnavailableError,
    async_fetch_active_mask,
)
from .command import RequestDeduplicator
from .const import (
    CONF_CONFIG_REVISION,
    CONF_DEVICE_UID,
    CONF_DISPLAY_NAME,
    CONF_MANIFEST_ACTION,
    CONF_MAPPINGS,
    CONF_VALIDATED_CONFIG_HASH,
    DOMAIN,
    page_device_identifier,
)
from .manifest_model import (
    ManifestError,
    RuntimeManifest,
    validate_runtime_manifest_templates,
)
from .mapping import (
    MappingError,
    effective_entry_data,
    persist_manifest_action,
    persist_runtime_validation,
    revalidate_persistent_mappings,
    validate_persistent_mappings,
)
from .page_order import (
    PageOrderObservation,
    PageOrderTransportResponseError,
    PageOrderTransportUnavailableError,
    async_fetch_page_order,
)
from .permission_check import (
    PERMISSION_CHECK_SCHEMA,
    SERVICE_PERMISSION_CHECK,
    async_handle_permission_check,
)
from .service import (
    COMMAND_SCHEMA,
    SERVICE_COMMAND,
    async_handle_command,
)
from .snapshot_recovery import (
    SERVICE_SNAPSHOT_REQUEST,
    SNAPSHOT_REQUEST_SCHEMA,
    async_handle_snapshot_request,
)
from .sync import (
    DisplaySyncError,
    DisplaySyncUnavailableError,
    EntryDisplaySync,
)
from .system import (
    HELLO_SCHEMA,
    SERVICE_HELLO,
    SERVICE_SNAPSHOT_ACK,
    SNAPSHOT_ACK_SCHEMA,
    async_handle_hello,
    async_handle_snapshot_ack,
    validate_hello_for_manifest,
)
from .transport import (
    ManifestTransportResponseError,
    ManifestTransportUnavailableError,
    async_fetch_runtime_manifest,
    async_find_manifest_actions_for_device,
    async_request_hello,
)

__all__ = ["DOMAIN"]

PLATFORMS = [Platform.SENSOR]
_LOGGER = logging.getLogger(__name__)


@dataclass
class MatouchControllerData:
    """Runtime data shared by loaded MaTouch config entries."""

    entries: dict[str, ConfigEntry] = field(default_factory=dict)
    manifests: dict[str, RuntimeManifest] = field(default_factory=dict)
    syncs: dict[str, EntryDisplaySync] = field(default_factory=dict)
    config_revisions: dict[str, int] = field(default_factory=dict)
    # Manifest action the published runtime was set up with (HA §9.8 pt. 8).
    manifest_actions: dict[str, str] = field(default_factory=dict)
    active_masks: dict[str, ActiveMaskObservation] = field(default_factory=dict)
    page_orders: dict[str, PageOrderObservation] = field(default_factory=dict)
    known_boot_ids: dict[str, str] = field(default_factory=dict)
    pending_hellos: dict[str, dict[str, object]] = field(default_factory=dict)
    # The pending hello that last allowed a C2 reassociation pass, per UID.
    reassociation_hellos: dict[str, dict[str, object]] = field(default_factory=dict)
    deduplicator: RequestDeduplicator = field(
        default_factory=RequestDeduplicator
    )


def _runtime_data(hass: HomeAssistant) -> MatouchControllerData:
    data = hass.data.get(DOMAIN)
    if isinstance(data, MatouchControllerData):
        return data

    data = MatouchControllerData()
    hass.data[DOMAIN] = data
    return data


def _configured_entry_for_device_uid(
    hass: HomeAssistant,
    device_uid: str,
) -> ConfigEntry | None:
    """Return the persistent MaTouch Config Entry for one device identity."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        effective = effective_entry_data(entry)
        if effective.get(CONF_DEVICE_UID) == device_uid:
            return entry
    return None


def _compiled_mask_for_manifest(manifest: RuntimeManifest) -> int:
    """Return the functional compiled mask represented by one runtime manifest."""
    mask = 0
    for page in manifest.pages:
        mask |= 1 << (page.page_slot - 1)
    return mask


async def _async_refresh_active_mask_state(
    hass: HomeAssistant,
    data: MatouchControllerData,
    *,
    device_uid: str,
    manifest: RuntimeManifest,
    manifest_action: str,
    sync: EntryDisplaySync | None = None,
) -> None:
    """Refresh ephemeral observed active-mask state without owning device state."""
    if sync is not None and (sync._stopped or data.syncs.get(device_uid) is not sync):
        return
    previous = data.active_masks.get(device_uid, ActiveMaskObservation())
    try:
        state = await async_fetch_active_mask(hass, manifest_action)
    except (
        ActiveMaskTransportUnavailableError,
        ActiveMaskTransportResponseError,
    ):
        if sync is not None and (sync._stopped or data.syncs.get(device_uid) is not sync):
            return
        data.active_masks[device_uid] = previous.marked_stale()
        return

    if sync is not None and (sync._stopped or data.syncs.get(device_uid) is not sync):
        return
    if state.compiled_mask != _compiled_mask_for_manifest(manifest):
        data.active_masks[device_uid] = previous.marked_stale()
        return

    data.active_masks[device_uid] = ActiveMaskObservation.from_state(state)


async def _async_refresh_page_order_state(
    hass: HomeAssistant,
    data: MatouchControllerData,
    *,
    device_uid: str,
    manifest: RuntimeManifest,
    manifest_action: str,
    sync: EntryDisplaySync | None = None,
) -> None:
    """Refresh ephemeral observed page order without owning device state."""
    if sync is not None and (sync._stopped or data.syncs.get(device_uid) is not sync):
        return
    previous = data.page_orders.get(device_uid, PageOrderObservation())
    try:
        state = await async_fetch_page_order(hass, manifest_action)
    except (
        PageOrderTransportUnavailableError,
        PageOrderTransportResponseError,
    ):
        if sync is not None and (sync._stopped or data.syncs.get(device_uid) is not sync):
            return
        data.page_orders[device_uid] = previous.marked_stale()
        return

    if sync is not None and (sync._stopped or data.syncs.get(device_uid) is not sync):
        return
    if state.compiled_mask != _compiled_mask_for_manifest(manifest):
        data.page_orders[device_uid] = previous.marked_stale()
        return

    data.page_orders[device_uid] = PageOrderObservation.from_state(state)


def _schedule_active_mask_refresh(
    hass: HomeAssistant,
    data: MatouchControllerData,
    *,
    device_uid: str,
) -> None:
    """Schedule a non-mutating active-mask refresh after an accepted reconnect."""
    entry = data.entries.get(device_uid)
    manifest = data.manifests.get(device_uid)
    if entry is None or manifest is None:
        return

    manifest_action = effective_entry_data(entry).get(CONF_MANIFEST_ACTION)
    if not isinstance(manifest_action, str) or not manifest_action:
        return

    hass.async_create_task(
        _async_refresh_active_mask_state(
            hass,
            data,
            device_uid=device_uid,
            manifest=manifest,
            manifest_action=manifest_action,
            sync=data.syncs.get(device_uid),
        ),
        f"MaTouch active-mask refresh {device_uid}",
    )


def _schedule_page_order_refresh(
    hass: HomeAssistant,
    data: MatouchControllerData,
    *,
    device_uid: str,
) -> None:
    """Schedule a non-mutating page-order refresh after accepted reconnect."""
    entry = data.entries.get(device_uid)
    manifest = data.manifests.get(device_uid)
    if entry is None or manifest is None:
        return

    manifest_action = effective_entry_data(entry).get(CONF_MANIFEST_ACTION)
    if not isinstance(manifest_action, str) or not manifest_action:
        return

    hass.async_create_task(
        _async_refresh_page_order_state(
            hass,
            data,
            device_uid=device_uid,
            manifest=manifest,
            manifest_action=manifest_action,
            sync=data.syncs.get(device_uid),
        ),
        f"MaTouch page-order refresh {device_uid}",
    )


def _runtime_update_requires_reload(
    data: MatouchControllerData,
    effective: dict[str, object],
) -> bool:
    """Return whether an entry update changes loaded display runtime state."""
    device_uid = effective.get(CONF_DEVICE_UID)
    if not isinstance(device_uid, str) or not device_uid:
        return True

    manifest = data.manifests.get(device_uid)
    sync = data.syncs.get(device_uid)
    loaded_revision = data.config_revisions.get(device_uid)
    if manifest is None or sync is None or loaded_revision is None:
        return True

    loaded_action = data.manifest_actions.get(device_uid)
    return (
        effective.get(CONF_CONFIG_REVISION) != loaded_revision
        or effective.get(CONF_VALIDATED_CONFIG_HASH) != manifest.config_hash
        or effective.get(CONF_MAPPINGS) != sync.mappings
        # A Config Flow reassociation changes only the transport (HA §9.8 pt. 8).
        or (
            loaded_action is not None
            and effective.get(CONF_MANIFEST_ACTION) != loaded_action
        )
    )


async def _async_reload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Reload when Config Entry changes affect display runtime or presentation."""
    effective = effective_entry_data(entry)
    data = _runtime_data(hass)
    if not _runtime_update_requires_reload(data, effective):
        _sync_entry_title(hass, entry, effective)
        return

    await hass.config_entries.async_reload(entry.entry_id)


def _sync_entry_title(
    hass: HomeAssistant,
    entry: ConfigEntry,
    effective: dict[str, object],
) -> None:
    """Keep the visible config entry title aligned with its human name."""
    display_name = effective.get(CONF_DISPLAY_NAME)
    if not isinstance(display_name, str) or not display_name.strip():
        return

    normalized = display_name.strip()
    if getattr(entry, "title", None) == normalized:
        return

    hass.config_entries.async_update_entry(entry, title=normalized)


async def async_setup(
    hass: HomeAssistant,
    config: ConfigType,
) -> bool:
    """Set up the integration and register its device command action."""
    data = _runtime_data(hass)

    async def handle_permission_check(call: ServiceCall) -> dict[str, object]:
        return await async_handle_permission_check(call, data.syncs)

    async def handle_command(call: ServiceCall) -> None:
        await async_handle_command(
            hass,
            call,
            data.entries,
            data.manifests,
            data.deduplicator,
            data.syncs,
        )

    async def handle_hello(call: ServiceCall) -> None:
        device_uid = call.data.get(CONF_DEVICE_UID)
        boot_id = call.data.get("boot_id")
        configured_entry = None
        if (
            isinstance(device_uid, str)
            and device_uid
            and device_uid not in data.syncs
        ):
            configured_entry = _configured_entry_for_device_uid(hass, device_uid)
        if (
            isinstance(device_uid, str)
            and device_uid
            and isinstance(boot_id, str)
            and boot_id
            and configured_entry is not None
        ):
            # The ESPHome connection may become ready while the Config Entry is
            # still in ConfigEntryNotReady backoff. Preserve the complete hello
            # so the setup retry can validate it against the fresh manifest and
            # complete the same boot handshake without a manual reload.
            data.pending_hellos[device_uid] = dict(call.data)

            # A device hello is stronger recovery evidence than Core's generic
            # exponential ConfigEntryNotReady timer. Use Core's public reload
            # scheduler so SETUP_RETRY is cancelled and transitioned legally
            # before the entry is set up again.
            if configured_entry.state is ConfigEntryState.SETUP_RETRY:
                hass.config_entries.async_schedule_reload(configured_entry.entry_id)

        accepted = await async_handle_hello(
            hass,
            call,
            data.entries,
            data.manifests,
            data.syncs,
            data.pending_hellos,
        )
        if not accepted:
            return

        if isinstance(device_uid, str) and isinstance(boot_id, str):
            sync = data.syncs.get(device_uid)
            if sync is not None and sync.active_boot_id == boot_id:
                data.known_boot_ids[device_uid] = boot_id
                data.pending_hellos.pop(device_uid, None)
                _schedule_active_mask_refresh(
                    hass,
                    data,
                    device_uid=device_uid,
                )
                _schedule_page_order_refresh(
                    hass,
                    data,
                    device_uid=device_uid,
                )

    async def handle_snapshot_ack(call: ServiceCall) -> None:
        await async_handle_snapshot_ack(
            call,
            data.entries,
            data.manifests,
            data.syncs,
        )

    async def handle_snapshot_request(call: ServiceCall) -> None:
        await async_handle_snapshot_request(
            hass,
            call,
            data.entries,
            data.manifests,
            data.syncs,
        )

    async def handle_page_device_registry_update(event: Event) -> None:
        """Immediately resync the owning display when a page device is renamed."""
        event_data = event.data
        if event_data.get("action") != "update":
            return
        changes = event_data.get("changes")
        if not isinstance(changes, dict) or not (
            "name_by_user" in changes or "name" in changes
        ):
            return

        device_id = event_data.get("device_id")
        if not isinstance(device_id, str):
            return

        registry = dr.async_get(hass)
        for device_uid, sync in data.syncs.items():
            entry = data.entries.get(device_uid)
            if entry is None:
                continue
            for page in sync.manifest.pages:
                child = registry.async_get_child_device_by_identifier(
                    (DOMAIN, page_device_identifier(device_uid, page.page_slot)),
                    entry.entry_id,
                )
                if child is not None and child.id == device_id:
                    try:
                        await sync.async_send_snapshot()
                    except DisplaySyncError:
                        return
                    return

    hass.services.async_register(
        DOMAIN,
        SERVICE_COMMAND,
        handle_command,
        schema=COMMAND_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PERMISSION_CHECK,
        handle_permission_check,
        schema=PERMISSION_CHECK_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_HELLO,
        handle_hello,
        schema=HELLO_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SNAPSHOT_ACK,
        handle_snapshot_ack,
        schema=SNAPSHOT_ACK_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SNAPSHOT_REQUEST,
        handle_snapshot_request,
        schema=SNAPSHOT_REQUEST_SCHEMA,
    )
    hass.bus.async_listen(
        dr.EVENT_DEVICE_REGISTRY_UPDATED,
        handle_page_device_registry_update,
    )
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Validate the active manifest and load one MaTouch config entry."""
    device_uid = effective_entry_data(entry).get(CONF_DEVICE_UID)
    if not isinstance(device_uid, str) or not device_uid:
        return False
    sync = await _async_setup_runtime(hass, entry)
    data = _runtime_data(hass)
    device_uid = sync.manifest.device_uid
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        sync.invalidate()
        _remove_sync_runtime(data, device_uid, sync)
        raise
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    _schedule_setup_hello_request(hass, data, sync)
    return True


def _schedule_setup_hello_request(
    hass: HomeAssistant, data: MatouchControllerData, sync: EntryDisplaySync,
) -> None:
    """Schedule once after successful setup; keep the original runtime owner."""
    if sync.active_boot_id is not None:
        return
    device_uid = sync.manifest.device_uid
    action = effective_entry_data(sync.entry)[CONF_MANIFEST_ACTION]

    async def request() -> None:
        # A hello or unload/replacement may have won before the task ran.
        if (
            sync._stopped
            or data.syncs.get(device_uid) is not sync
            or sync.active_boot_id is not None
        ):
            return
        try:
            await async_request_hello(hass, action)
        except ManifestTransportUnavailableError as err:
            if not sync._stopped and data.syncs.get(device_uid) is sync:
                _LOGGER.warning(
                    "Unable to request MaTouch hello for %s: %s", device_uid, err,
                )

    hass.async_create_task(request(), f"MaTouch setup hello {device_uid}")


def _remove_sync_runtime(
    data: MatouchControllerData, device_uid: str, sync: EntryDisplaySync,
) -> None:
    """Remove only the runtime still owned by this sync."""
    if data.syncs.get(device_uid) is sync:
        data.entries.pop(device_uid, None)
        data.manifests.pop(device_uid, None)
        data.syncs.pop(device_uid, None)
        data.config_revisions.pop(device_uid, None)
        data.manifest_actions.pop(device_uid, None)
        data.active_masks.pop(device_uid, None)
        data.page_orders.pop(device_uid, None)


async def _async_reassociate_transport(
    hass: HomeAssistant,
    data: MatouchControllerData,
    entry: ConfigEntry,
    *,
    device_uid: str,
    persisted_action: str,
) -> tuple[RuntimeManifest, str]:
    """Reassociate the HA -> display transport for the same UID (HA §9.8).

    Runs only during entry setup, after the persisted action proved unusable,
    and only with a complete hello received for this device_uid. Each hello
    allows at most one pass; the pending hello itself stays available to the
    existing deferred-hello bootstrap (Protocol §12.1.2).
    """
    pending_hello = data.pending_hellos.get(device_uid)
    if pending_hello is None:
        raise ConfigEntryNotReady(
            "Runtime manifest action is unavailable; waiting for a device hello"
        )
    if data.reassociation_hellos.get(device_uid) is pending_hello:
        raise ConfigEntryNotReady(
            "Runtime manifest action is unavailable; this hello was already resolved"
        )
    data.reassociation_hellos[device_uid] = pending_hello

    matches = await async_find_manifest_actions_for_device(
        hass, device_uid, exclude=persisted_action,
    )
    if not matches:
        raise ConfigEntryNotReady(
            "No ESPHome manifest action answers for this device_uid"
        )
    if len(matches) > 1:
        _LOGGER.error(
            "Ambiguous MaTouch transport for %s: %s; nothing was changed. "
            "Resolve manually, then reload the entry",
            device_uid,
            ", ".join(action for action, _ in matches),
        )
        raise ConfigEntryError(
            "Ambiguous runtime manifest actions for this device_uid"
        )

    new_action, manifest = matches[0]
    persist_manifest_action(hass, entry, new_action)
    _LOGGER.info(
        "MaTouch transport for %s reassociated: %s -> %s",
        device_uid,
        persisted_action,
        new_action,
    )
    return manifest, new_action


async def _async_setup_runtime(
    hass: HomeAssistant, entry: ConfigEntry,
    *, retained_sync: EntryDisplaySync | None = None,
) -> EntryDisplaySync:
    """Build fresh sync; retained platforms/listeners are never forwarded again."""
    effective = effective_entry_data(entry)
    device_uid = effective.get(CONF_DEVICE_UID)
    if not isinstance(device_uid, str) or not device_uid:
        raise ConfigEntryError("Invalid device_uid")

    data = _runtime_data(hass)
    action = effective.get(CONF_MANIFEST_ACTION)
    if not isinstance(action, str) or not action:
        raise ConfigEntryError("Missing runtime manifest action")

    manifest: RuntimeManifest | None
    try:
        manifest = await async_fetch_runtime_manifest(hass, action)
    except ManifestTransportUnavailableError:
        # Absent or failed persisted action (HA §9.8 pt. 2).
        manifest = None
    except ManifestTransportResponseError as err:
        raise ConfigEntryError("Runtime manifest is invalid") from err

    if manifest is not None and manifest.device_uid != device_uid:
        # The persisted action now belongs to another unit (HA §9.8 pt. 2).
        manifest = None

    if manifest is None:
        manifest, action = await _async_reassociate_transport(
            hass, data, entry, device_uid=device_uid, persisted_action=action,
        )
    else:
        try:
            validate_runtime_manifest_templates(manifest)
        except ManifestError as err:
            raise ConfigEntryError("Runtime manifest is invalid") from err

    try:
        if effective.get(CONF_VALIDATED_CONFIG_HASH) == manifest.config_hash:
            mappings = validate_persistent_mappings(
                manifest,
                effective.get(CONF_MAPPINGS),
            )
        else:
            mappings = revalidate_persistent_mappings(
                manifest,
                effective.get(CONF_MAPPINGS),
            )
    except MappingError as err:
        raise ConfigEntryError(
            "Persistent mappings are incompatible with runtime manifest"
        ) from err

    if retained_sync is not None and (
        manifest != retained_sync.manifest or mappings != retained_sync.mappings
    ):
        # Existing diagnostic entities still describe this topology/mapping.
        # A failed unload cannot safely replace those entities in place.
        raise ConfigEntryError("Retained platforms require unchanged manifest/mappings")

    _sync_entry_title(hass, entry, effective)
    persist_runtime_validation(
        hass,
        entry,
        config_hash=manifest.config_hash,
        mappings=mappings,
    )

    boot_id = data.known_boot_ids.get(device_uid)
    pending_hello = data.pending_hellos.get(device_uid)
    if pending_hello is not None:
        try:
            validate_hello_for_manifest(manifest, pending_hello)
        except ServiceValidationError:
            data.pending_hellos.pop(device_uid, None)
        else:
            pending_boot_id = pending_hello.get("boot_id")
            if isinstance(pending_boot_id, str) and pending_boot_id:
                boot_id = pending_boot_id

    sync = EntryDisplaySync(hass, entry, manifest, mappings)
    if boot_id is not None:
        sync.begin_device_handshake(boot_id)

    # Publish the runtime before sending any snapshot so device-originated ACKs
    # can resolve the same sync instance while the blocking ESPHome action chain
    # is still in progress.
    data.entries[device_uid] = entry
    data.manifests[device_uid] = manifest
    data.syncs[device_uid] = sync
    data.config_revisions[device_uid] = sync.config_revision
    data.manifest_actions[device_uid] = action

    try:
        await sync.async_start(send_snapshot=boot_id is not None)
    except DisplaySyncUnavailableError as err:
        await sync.async_stop()
        if data.syncs.get(device_uid) is sync:
            data.entries.pop(device_uid, None)
            data.manifests.pop(device_uid, None)
            data.syncs.pop(device_uid, None)
            data.config_revisions.pop(device_uid, None)
            data.manifest_actions.pop(device_uid, None)
            data.active_masks.pop(device_uid, None)
            data.page_orders.pop(device_uid, None)
        raise ConfigEntryNotReady(
            "Display synchronization actions are temporarily unavailable"
        ) from err
    except DisplaySyncError as err:
        await sync.async_stop()
        if data.syncs.get(device_uid) is sync:
            data.entries.pop(device_uid, None)
            data.manifests.pop(device_uid, None)
            data.syncs.pop(device_uid, None)
            data.config_revisions.pop(device_uid, None)
            data.manifest_actions.pop(device_uid, None)
            data.active_masks.pop(device_uid, None)
            data.page_orders.pop(device_uid, None)
        raise ConfigEntryError("Display synchronization setup failed") from err
    except BaseException:
        sync.invalidate()
        _remove_sync_runtime(data, device_uid, sync)
        raise

    try:
        await _async_refresh_active_mask_state(
            hass,
            data,
            device_uid=device_uid,
            manifest=manifest,
            manifest_action=action,
            sync=sync,
        )
        await _async_refresh_page_order_state(
            hass,
            data,
            device_uid=device_uid,
            manifest=manifest,
            manifest_action=action,
            sync=sync,
        )

        sync._require_lifecycle_active()
        if data.syncs.get(device_uid) is not sync:
            raise DisplaySyncError("Runtime replaced during setup")
    except BaseException:
        sync.invalidate()
        _remove_sync_runtime(data, device_uid, sync)
        raise

    if sync.active_boot_id is not None:
        data.known_boot_ids[device_uid] = sync.active_boot_id
        data.pending_hellos.pop(device_uid, None)
    data.reassociation_hellos.pop(device_uid, None)

    return sync


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Unload a MaTouch Controller config entry."""
    effective = effective_entry_data(entry)
    device_uid = effective.get(CONF_DEVICE_UID)
    data = hass.data.get(DOMAIN)
    sync: EntryDisplaySync | None = None

    if isinstance(data, MatouchControllerData) and isinstance(device_uid, str):
        sync = data.syncs.get(device_uid)
        if sync is not None:
            active_boot_id = sync.active_boot_id
            if active_boot_id is not None:
                data.known_boot_ids[device_uid] = active_boot_id

            # This synchronous fence must happen before the first await of the
            # unload path. It permanently closes normal-runtime work from this
            # object, including work already suspended or merely scheduled.
            sync.invalidate()
            if data.syncs.get(device_uid) is sync:
                data.syncs.pop(device_uid, None)

            try:
                await sync.async_send_shutdown_configuration_state()
            except DisplaySyncError:
                # Shutdown notification is best-effort; lifecycle invalidation
                # has already happened and unload must continue.
                pass

    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        if isinstance(data, MatouchControllerData) and sync is not None:
            try:
                restored = await _async_setup_runtime(hass, entry, retained_sync=sync)
                _schedule_setup_hello_request(hass, data, restored)
            except Exception:
                # No live sync means services reject commands and retained
                # runtime diagnostics are unavailable. The update listener is
                # retained so an explicit reload can retry normal setup.
                _LOGGER.exception("Unable to restore MaTouch sync after refused unload")
        return False

    if isinstance(data, MatouchControllerData) and isinstance(device_uid, str):
        if sync is not None:
            await sync.async_stop()
        if data.entries.get(device_uid) is entry:
            data.entries.pop(device_uid, None)
        data.manifests.pop(device_uid, None)
        data.config_revisions.pop(device_uid, None)
        data.manifest_actions.pop(device_uid, None)
        data.active_masks.pop(device_uid, None)
        data.page_orders.pop(device_uid, None)

    return True


