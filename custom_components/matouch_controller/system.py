"""Reconnect handshake services for MaTouch devices."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, MutableMapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_DEVICE_UID,
    CONF_MANIFEST_ACTION,
    CONF_VALIDATED_CONFIG_HASH,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
)
from .manifest_model import (
    ManifestError,
    RuntimeManifest,
    validate_runtime_manifest_templates,
)
from .mapping import effective_entry_data
from .sync import DisplaySyncError, EntryDisplaySync
from .transport import (
    ManifestTransportResponseError,
    ManifestTransportUnavailableError,
    async_fetch_runtime_manifest,
)

SERVICE_HELLO = "hello"
SERVICE_SNAPSHOT_ACK = "snapshot_ack"

ERROR_INVALID_PAYLOAD = "E_INVALID_PAYLOAD"
ERROR_NOT_CONFIGURED = "E_NOT_CONFIGURED"
ERROR_IDENTITY_MISMATCH = "E_IDENTITY_MISMATCH"
ERROR_PROTOCOL_MISMATCH = "E_PROTOCOL_MISMATCH"
ERROR_SCHEMA_MISMATCH = "E_SCHEMA_MISMATCH"
ERROR_CONFIG_CHANGED = "E_CONFIG_CHANGED"
ERROR_STALE_REVISION = "E_STALE_REVISION"

_BOOT_ID_PATTERN = re.compile(r"^[0-9a-f]{8}$")
_LOGGER = logging.getLogger(__name__)


def _boot_id(value: Any) -> str:
    if not isinstance(value, str) or _BOOT_ID_PATTERN.fullmatch(value) is None:
        raise vol.Invalid("expected 8 lowercase hexadecimal characters")
    return value


def _positive_revision(value: Any) -> int:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise vol.Invalid("expected positive integer")
    return value


HELLO_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_UID): cv.string,
        vol.Required("product_profile"): cv.string,
        vol.Required("protocol_version"): cv.string,
        vol.Required("schema_version"): cv.string,
        vol.Required("firmware_version"): cv.string,
        vol.Required("template_library_version"): cv.string,
        vol.Required("config_hash"): cv.string,
        vol.Required("boot_id"): _boot_id,
    }
)

SNAPSHOT_ACK_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_UID): cv.string,
        vol.Required("boot_id"): _boot_id,
        vol.Required("integration_session_id"): cv.string,
        vol.Required("config_revision"): _positive_revision,
        vol.Required("state_revision"): _positive_revision,
    }
)


def _loaded_runtime(
    call: ServiceCall,
    entries: Mapping[str, ConfigEntry],
    manifests: Mapping[str, RuntimeManifest],
    syncs: Mapping[str, EntryDisplaySync],
) -> tuple[str, ConfigEntry, RuntimeManifest, EntryDisplaySync]:
    device_uid = call.data.get(CONF_DEVICE_UID)
    if not isinstance(device_uid, str) or not device_uid:
        raise ServiceValidationError(
            f"{ERROR_INVALID_PAYLOAD}: Invalid or missing device_uid"
        )

    entry = entries.get(device_uid)
    manifest = manifests.get(device_uid)
    sync = syncs.get(device_uid)
    if entry is None or manifest is None or sync is None:
        raise ServiceValidationError(
            f"{ERROR_NOT_CONFIGURED}: No loaded runtime for {device_uid}"
        )
    return device_uid, entry, manifest, sync


_HELLO_MANIFEST_FIELDS = (
    "product_profile",
    "protocol_version",
    "schema_version",
    "firmware_version",
    "template_library_version",
    "config_hash",
)


def _hello_matches_manifest(
    manifest: RuntimeManifest,
    data: Mapping[str, Any],
) -> bool:
    """Return whether hello describes the cached active manifest exactly."""
    return all(
        data.get(field) == getattr(manifest, field)
        for field in _HELLO_MANIFEST_FIELDS
    )


def _validate_hello_compatibility(data: Mapping[str, Any]) -> None:
    """Reject protocol/schema versions that the integration cannot support."""
    protocol = data.get("protocol_version")
    if (
        not isinstance(protocol, str)
        or protocol.split(".", 1)[0] != PROTOCOL_VERSION.split(".", 1)[0]
    ):
        raise ServiceValidationError(
            "E_PROTOCOL_INCOMPATIBLE: Unsupported protocol_version"
        )

    if data.get("schema_version") != SCHEMA_VERSION:
        raise ServiceValidationError(
            "E_SCHEMA_INCOMPATIBLE: Unsupported schema_version"
        )


async def _async_fetch_current_manifest(
    hass: HomeAssistant,
    *,
    device_uid: str,
    entry: ConfigEntry,
    cached_manifest: RuntimeManifest,
) -> RuntimeManifest:
    """Fetch and validate the manifest exposed by the currently connected firmware."""
    action = effective_entry_data(entry).get(CONF_MANIFEST_ACTION)
    if not isinstance(action, str) or not action:
        raise ServiceValidationError(
            f"{ERROR_NOT_CONFIGURED}: Missing runtime manifest action"
        )

    try:
        refreshed = await async_fetch_runtime_manifest(hass, action)
        validate_runtime_manifest_templates(refreshed)
    except (
        ManifestTransportUnavailableError,
        ManifestTransportResponseError,
        ManifestError,
    ) as err:
        raise ServiceValidationError(
            f"{ERROR_NOT_CONFIGURED}: Unable to refresh runtime manifest"
        ) from err

    if refreshed.device_uid != device_uid:
        raise ServiceValidationError(
            f"{ERROR_IDENTITY_MISMATCH}: Refreshed manifest device_uid changed"
        )
    if refreshed.product_profile != cached_manifest.product_profile:
        raise ServiceValidationError(
            f"{ERROR_IDENTITY_MISMATCH}: Refreshed manifest product_profile changed"
        )

    return refreshed


def _validate_hello(manifest: RuntimeManifest, data: Mapping[str, Any]) -> None:
    comparisons = (
        ("product_profile", ERROR_IDENTITY_MISMATCH),
        ("protocol_version", ERROR_PROTOCOL_MISMATCH),
        ("schema_version", ERROR_SCHEMA_MISMATCH),
        ("firmware_version", ERROR_IDENTITY_MISMATCH),
        ("template_library_version", ERROR_IDENTITY_MISMATCH),
        ("config_hash", ERROR_CONFIG_CHANGED),
    )
    for field, code in comparisons:
        if data.get(field) != getattr(manifest, field):
            raise ServiceValidationError(
                f"{code}: hello {field} does not match active manifest"
            )


def validate_hello_for_manifest(
    manifest: RuntimeManifest,
    data: Mapping[str, Any],
) -> None:
    """Validate a previously received hello against one fresh active manifest."""
    _validate_hello_compatibility(data)
    _validate_hello(manifest, data)


async def _async_resync(
    sync: EntryDisplaySync,
    device_uid: str,
    transport_generation: int,
) -> None:
    try:
        await sync.async_send_snapshot(
            transport_generation=transport_generation,
        )
    except DisplaySyncError:
        _LOGGER.exception("Reconnect snapshot failed for %s", device_uid)


async def _async_reload_for_manifest(
    hass: HomeAssistant,
    *,
    device_uid: str,
    entry: ConfigEntry,
    hello: Mapping[str, Any],
    pending_hellos: MutableMapping[str, dict[str, object]] | None,
) -> None:
    """Preserve the validated hello and rebuild runtime through one entry reload."""
    if pending_hellos is not None:
        pending_hellos[device_uid] = dict(hello)

    _LOGGER.info(
        "Reloading MaTouch runtime for validated manifest change on %s",
        device_uid,
    )
    await hass.config_entries.async_reload(entry.entry_id)


async def async_handle_hello(
    hass: HomeAssistant,
    call: ServiceCall,
    entries: Mapping[str, ConfigEntry],
    manifests: Mapping[str, RuntimeManifest],
    syncs: Mapping[str, EntryDisplaySync],
    pending_hellos: MutableMapping[str, dict[str, object]] | None = None,
) -> bool:
    """Validate reconnect hello; resync unchanged runtime or reload changed runtime.

    Return true only when this call accepted the hello on the currently loaded
    runtime and scheduled its snapshot. A validated manifest/configuration change
    returns false after requesting one Config Entry reload; setup then owns fresh
    mapping revalidation, topology construction and snapshot bootstrap.
    """
    device_uid, entry, manifest, sync = _loaded_runtime(
        call,
        entries,
        manifests,
        syncs,
    )
    generation = sync._transport_generation

    def require_current_runtime() -> None:
        """Do not reload or accept hello after an awaited runtime replacement."""
        try:
            sync._require_transport_generation(generation)
        except DisplaySyncError as err:
            raise ServiceValidationError(f"{ERROR_STALE_REVISION}: {err}") from err
        if syncs.get(device_uid) is not sync:
            raise ServiceValidationError(f"{ERROR_STALE_REVISION}: Runtime replaced")

    require_current_runtime()
    _validate_hello_compatibility(call.data)

    if not _hello_matches_manifest(manifest, call.data):
        refreshed = await _async_fetch_current_manifest(
            hass,
            device_uid=device_uid,
            entry=entry,
            cached_manifest=manifest,
        )
        require_current_runtime()
        _validate_hello(refreshed, call.data)

        # Never mutate the loaded manifest/sync in place. Unsupported manifests
        # have already failed above; a supported changed manifest is rebuilt by
        # normal Config Entry setup so page/control topology is reconstructed
        # from fresh data and surviving mappings are revalidated exactly once.
        if refreshed != manifest:
            await _async_reload_for_manifest(
                hass,
                device_uid=device_uid,
                entry=entry,
                hello=call.data,
                pending_hellos=pending_hellos,
            )
            return False

    effective = effective_entry_data(entry)
    if effective.get(CONF_VALIDATED_CONFIG_HASH) != manifest.config_hash:
        await _async_reload_for_manifest(
            hass,
            device_uid=device_uid,
            entry=entry,
            hello=call.data,
            pending_hellos=pending_hellos,
        )
        return False

    _validate_hello(manifest, call.data)
    transport_generation = sync.begin_device_handshake(call.data["boot_id"])
    _LOGGER.info(
        "Accepted MaTouch hello boot_id=%s for %s",
        call.data["boot_id"],
        device_uid,
    )
    hass.async_create_task(
        _async_resync(sync, device_uid, transport_generation),
        f"MaTouch reconnect snapshot {device_uid}",
    )
    return True


async def async_handle_snapshot_ack(
    call: ServiceCall,
    entries: Mapping[str, ConfigEntry],
    manifests: Mapping[str, RuntimeManifest],
    syncs: Mapping[str, EntryDisplaySync],
) -> None:
    """Accept the ACK for the currently pending complete snapshot."""
    device_uid, _entry, _manifest, sync = _loaded_runtime(
        call,
        entries,
        manifests,
        syncs,
    )
    try:
        sync.require_active_boot(call.data["boot_id"])
        await sync.async_ack_snapshot(
            integration_session_id=call.data["integration_session_id"],
            config_revision=call.data["config_revision"],
            state_revision=call.data["state_revision"],
        )
    except DisplaySyncError as err:
        raise ServiceValidationError(
            f"{ERROR_STALE_REVISION}: {err}"
        ) from err

    _LOGGER.info(
        "Accepted MaTouch snapshot ACK boot_id=%s revision=%s for %s",
        call.data["boot_id"],
        call.data["state_revision"],
        device_uid,
    )

