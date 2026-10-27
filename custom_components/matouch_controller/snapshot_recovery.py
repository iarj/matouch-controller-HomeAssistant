"""Deterministic snapshot timeout recovery for MaTouch reconnect/resync."""

from __future__ import annotations

import logging
from collections.abc import Mapping

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .manifest_model import RuntimeManifest
from .sync import (
    DisplaySyncError,
    DisplaySyncUnavailableError,
    EntryDisplaySync,
)
from .system import _boot_id, _loaded_runtime, _positive_revision

SERVICE_SNAPSHOT_REQUEST = "snapshot_request"
ERROR_SNAPSHOT_TIMEOUT = "E_SNAPSHOT_TIMEOUT"
ERROR_STALE_REVISION = "E_STALE_REVISION"

SNAPSHOT_REQUEST_SCHEMA = vol.Schema(
    {
        vol.Required("device_uid"): cv.string,
        vol.Required("boot_id"): _boot_id,
        vol.Required("integration_session_id"): cv.string,
        vol.Required("config_revision"): _positive_revision,
        vol.Required("state_revision"): _positive_revision,
        vol.Required("error_code"): vol.In((ERROR_SNAPSHOT_TIMEOUT,)),
    }
)

_LOGGER = logging.getLogger(__name__)


async def async_recover_snapshot_timeout(
    sync: EntryDisplaySync,
    *,
    integration_session_id: str,
    config_revision: int,
    state_revision: int,
) -> None:
    """Replace exactly one timed-out pending snapshot with a fresh full snapshot.

    The sync owns serialization, including pending verification/replacement.
    Queued HA changes remain recoverable through the exact replacement ACK.
    """
    await sync.async_recover_snapshot(
        integration_session_id=integration_session_id,
        config_revision=config_revision,
        state_revision=state_revision,
    )


async def async_handle_snapshot_request(
    hass: HomeAssistant,
    call: ServiceCall,
    entries: Mapping[str, ConfigEntry],
    manifests: Mapping[str, RuntimeManifest],
    syncs: Mapping[str, EntryDisplaySync],
) -> None:
    """Recover one matching snapshot timeout without reloading the integration."""
    device_uid, _entry, _manifest, sync = _loaded_runtime(
        call,
        entries,
        manifests,
        syncs,
    )

    try:
        sync.require_active_boot(call.data["boot_id"])
        await async_recover_snapshot_timeout(
            sync,
            integration_session_id=call.data["integration_session_id"],
            config_revision=call.data["config_revision"],
            state_revision=call.data["state_revision"],
        )
    except DisplaySyncUnavailableError as err:
        raise ServiceValidationError(
            f"{ERROR_SNAPSHOT_TIMEOUT}: Unable to start replacement snapshot"
        ) from err
    except DisplaySyncError as err:
        raise ServiceValidationError(
            f"{ERROR_STALE_REVISION}: {err}"
        ) from err

    _LOGGER.warning(
        "%s recovery accepted for %s failed_revision=%s",
        ERROR_SNAPSHOT_TIMEOUT,
        device_uid,
        call.data["state_revision"],
    )

