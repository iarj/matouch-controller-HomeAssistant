"""Read-only connectivity query over the existing ESPHome action transport.

The echoed call identity confirms only the responding MaTouch integration and
permission to execute this query. It does not establish device sync readiness.
No runtime, mapping, registry, handshake or entity state is changed here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import CONF_DEVICE_UID, PROTOCOL_VERSION

SERVICE_PERMISSION_CHECK = "permission_check"


def _identity(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{8}", value) is None:
        raise vol.Invalid("expected 8 lowercase hexadecimal characters")
    return value


def _request(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{8}-check-[1-9][0-9]{0,9}", value) is None:
        raise vol.Invalid("expected bounded boot-check-counter identity")
    return value


PERMISSION_CHECK_SCHEMA = vol.Schema({
    vol.Required(CONF_DEVICE_UID): vol.All(cv.string, vol.Length(min=1, max=128)),
    vol.Required("boot_id"): _identity,
    vol.Required("request_id"): _request,
    vol.Required("protocol_version"): cv.string,
})


async def async_handle_permission_check(
    call: ServiceCall, syncs: Mapping[str, Any],
) -> dict[str, Any]:
    """Echo a valid query; an absent configured runtime still gets a response."""
    payload = PERMISSION_CHECK_SCHEMA(dict(call.data))
    boot = payload["boot_id"]
    if not payload["request_id"].startswith(boot + "-check-"):
        raise ServiceValidationError("E_IDENTITY_MISMATCH: query boot differs from request")
    if payload["protocol_version"] != PROTOCOL_VERSION:
        raise ServiceValidationError("E_PROTOCOL_MISMATCH: unsupported query protocol")
    sync = syncs.get(payload[CONF_DEVICE_UID])
    if sync is not None and sync.active_boot_id not in (None, boot):
        raise ServiceValidationError("E_STALE_BOOT: query boot differs from active runtime")
    return {
        "device_uid": payload[CONF_DEVICE_UID],
        "boot_id": boot,
        "request_id": payload["request_id"],
        "protocol_version": PROTOCOL_VERSION,
        "integration": "matouch_controller",
        "query_allowed": True,
    }
