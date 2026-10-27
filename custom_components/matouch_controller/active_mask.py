"""ESPHome transport for MaTouch active functional-page masks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError

from .transport import ESPHOME_DOMAIN, MANIFEST_ACTION_SUFFIX

ACTIVE_MASK_READ_ACTION_SUFFIX = "_matouch_get_active_mask"
ACTIVE_MASK_WRITE_ACTION_SUFFIX = "_matouch_set_active_mask"
ACTIVE_MASK_RESPONSE_TIMEOUT_SECONDS = 10
ACTIVE_MASK_FUNCTIONAL_BITS = 0x3F
ACTIVE_MASK_OK = "OK"


class ActiveMaskTransportError(HomeAssistantError):
    """Base error for active-mask transport failures."""


class ActiveMaskTransportUnavailableError(ActiveMaskTransportError):
    """Raised when an active-mask ESPHome action is unavailable."""


class ActiveMaskTransportResponseError(ActiveMaskTransportError):
    """Raised when an active-mask ESPHome response is invalid."""


@dataclass(frozen=True, slots=True)
class ActiveMaskState:
    """Validated device-authoritative active-mask state."""

    compiled_mask: int
    active_mask: int


@dataclass(frozen=True, slots=True)
class ActiveMaskWriteResult(ActiveMaskState):
    """Validated ACK + readback returned by a mutative active-mask write."""

    changed: bool
    error_code: str


@dataclass(frozen=True, slots=True)
class ActiveMaskObservation:
    """Ephemeral HA observation of device-authoritative active-mask state."""

    compiled_mask: int | None = None
    active_mask: int | None = None
    stale: bool = True
    error_code: str | None = None

    @classmethod
    def from_state(
        cls,
        state: ActiveMaskState,
        *,
        error_code: str = ACTIVE_MASK_OK,
    ) -> ActiveMaskObservation:
        """Create one fresh observation from a validated device response."""
        return cls(
            compiled_mask=state.compiled_mask,
            active_mask=state.active_mask,
            stale=False,
            error_code=error_code,
        )

    def marked_stale(self) -> ActiveMaskObservation:
        """Preserve the last observation while explicitly making it stale."""
        return ActiveMaskObservation(
            compiled_mask=self.compiled_mask,
            active_mask=self.active_mask,
            stale=True,
            error_code=None,
        )


def active_mask_actions_for_manifest(manifest_action: Any) -> tuple[str, str]:
    """Derive sibling active-mask actions from the selected manifest action."""
    if not isinstance(manifest_action, str):
        raise ActiveMaskTransportUnavailableError(
            "Manifest action must be a string"
        )

    domain, separator, service = manifest_action.partition(".")
    if (
        separator != "."
        or domain != ESPHOME_DOMAIN
        or not service.endswith(MANIFEST_ACTION_SUFFIX)
    ):
        raise ActiveMaskTransportUnavailableError(
            f"Invalid ESPHome manifest action: {manifest_action}"
        )

    prefix = service[: -len(MANIFEST_ACTION_SUFFIX)]
    if not prefix:
        raise ActiveMaskTransportUnavailableError(
            f"Invalid ESPHome manifest action: {manifest_action}"
        )

    return (
        f"{ESPHOME_DOMAIN}.{prefix}{ACTIVE_MASK_READ_ACTION_SUFFIX}",
        f"{ESPHOME_DOMAIN}.{prefix}{ACTIVE_MASK_WRITE_ACTION_SUFFIX}",
    )


def _split_action(action: str, suffix: str) -> tuple[str, str]:
    domain, separator, service = action.partition(".")
    if separator != "." or domain != ESPHOME_DOMAIN or not service.endswith(suffix):
        raise ActiveMaskTransportUnavailableError(
            f"Invalid ESPHome active-mask action: {action}"
        )
    return domain, service


def _action_available(hass: HomeAssistant, action: str) -> bool:
    domain, separator, service_name = action.partition(".")
    if separator != ".":
        return False
    service = hass.services.async_services().get(domain, {}).get(service_name)
    return (
        service is not None
        and service.supports_response is SupportsResponse.ONLY
    )


def _validated_mask_pair(response: dict[str, Any]) -> tuple[int, int]:
    compiled_mask = response.get("compiled_mask")
    active_mask = response.get("active_mask")

    if (
        isinstance(compiled_mask, bool)
        or not isinstance(compiled_mask, int)
        or compiled_mask <= 0
        or compiled_mask > ACTIVE_MASK_FUNCTIONAL_BITS
    ):
        raise ActiveMaskTransportResponseError(
            "Active-mask response has invalid compiled_mask"
        )

    if (
        isinstance(active_mask, bool)
        or not isinstance(active_mask, int)
        or active_mask <= 0
        or active_mask > ACTIVE_MASK_FUNCTIONAL_BITS
        or active_mask & ~compiled_mask
    ):
        raise ActiveMaskTransportResponseError(
            "Active-mask response has invalid active_mask"
        )

    return compiled_mask, active_mask


async def _async_call_action(
    hass: HomeAssistant,
    action: str,
    data: dict[str, Any],
    *,
    suffix: str,
) -> dict[str, Any]:
    domain, service = _split_action(action, suffix)
    if not _action_available(hass, action):
        raise ActiveMaskTransportUnavailableError(
            f"ESPHome active-mask action unavailable: {action}"
        )

    try:
        async with asyncio.timeout(ACTIVE_MASK_RESPONSE_TIMEOUT_SECONDS):
            response = await hass.services.async_call(
                domain,
                service,
                data,
                blocking=True,
                return_response=True,
            )
    except TimeoutError as err:
        raise ActiveMaskTransportUnavailableError(
            f"Timed out calling ESPHome active-mask action: {action}"
        ) from err
    except HomeAssistantError as err:
        raise ActiveMaskTransportUnavailableError(
            f"ESPHome active-mask action failed: {action}"
        ) from err

    if not isinstance(response, dict):
        raise ActiveMaskTransportResponseError(
            "ESPHome active-mask action returned no JSON object"
        )
    return response


async def async_fetch_active_mask(
    hass: HomeAssistant,
    manifest_action: str,
) -> ActiveMaskState:
    """Read and validate the device-authoritative active mask."""
    read_action, _ = active_mask_actions_for_manifest(manifest_action)
    response = await _async_call_action(
        hass,
        read_action,
        {},
        suffix=ACTIVE_MASK_READ_ACTION_SUFFIX,
    )
    compiled_mask, active_mask = _validated_mask_pair(response)
    if response.get("error_code") != ACTIVE_MASK_OK:
        raise ActiveMaskTransportResponseError(
            "ESPHome active-mask read did not return OK"
        )
    return ActiveMaskState(
        compiled_mask=compiled_mask,
        active_mask=active_mask,
    )


async def async_set_active_mask(
    hass: HomeAssistant,
    manifest_action: str,
    active_mask: int,
) -> ActiveMaskWriteResult:
    """Write one complete desired mask and validate the ACK + readback."""
    if (
        isinstance(active_mask, bool)
        or not isinstance(active_mask, int)
        or active_mask <= 0
        or active_mask > ACTIVE_MASK_FUNCTIONAL_BITS
    ):
        raise ActiveMaskTransportResponseError(
            "Requested active_mask must be a nonzero functional-bit mask"
        )

    _, write_action = active_mask_actions_for_manifest(manifest_action)
    response = await _async_call_action(
        hass,
        write_action,
        {"active_mask": active_mask},
        suffix=ACTIVE_MASK_WRITE_ACTION_SUFFIX,
    )
    compiled_mask, returned_active_mask = _validated_mask_pair(response)

    changed = response.get("changed")
    error_code = response.get("error_code")
    if not isinstance(changed, bool):
        raise ActiveMaskTransportResponseError(
            "Active-mask write response has invalid changed"
        )
    if not isinstance(error_code, str) or not error_code:
        raise ActiveMaskTransportResponseError(
            "Active-mask write response has invalid error_code"
        )
    if error_code != ACTIVE_MASK_OK and changed:
        raise ActiveMaskTransportResponseError(
            "Failed active-mask write cannot report changed=true"
        )

    return ActiveMaskWriteResult(
        compiled_mask=compiled_mask,
        active_mask=returned_active_mask,
        changed=changed,
        error_code=error_code,
    )
