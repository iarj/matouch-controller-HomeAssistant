"""ESPHome transport for MaTouch runtime functional page ordering."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError

from .transport import ESPHOME_DOMAIN, MANIFEST_ACTION_SUFFIX

PAGE_ORDER_READ_ACTION_SUFFIX = "_matouch_get_page_order"
PAGE_ORDER_WRITE_ACTION_SUFFIX = "_matouch_set_page_order"
PAGE_ORDER_RESPONSE_TIMEOUT_SECONDS = 10
PAGE_ORDER_FUNCTIONAL_BITS = 0x3F
PAGE_ORDER_OK = "OK"


class PageOrderTransportError(HomeAssistantError):
    """Base error for page-order transport failures."""


class PageOrderTransportUnavailableError(PageOrderTransportError):
    """Raised when a page-order ESPHome action is unavailable."""


class PageOrderTransportResponseError(PageOrderTransportError):
    """Raised when a page-order ESPHome response is invalid."""


@dataclass(frozen=True, slots=True)
class PageOrderState:
    """Validated device-authoritative functional page order."""

    compiled_mask: int
    page_order: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PageOrderWriteResult(PageOrderState):
    """Validated ACK + readback returned by a page-order write."""

    changed: bool
    error_code: str


@dataclass(frozen=True, slots=True)
class PageOrderObservation:
    """Ephemeral HA observation of device-authoritative page order."""

    compiled_mask: int | None = None
    page_order: tuple[int, ...] | None = None
    stale: bool = True
    error_code: str | None = None

    @classmethod
    def from_state(
        cls,
        state: PageOrderState,
        *,
        error_code: str = PAGE_ORDER_OK,
    ) -> PageOrderObservation:
        return cls(
            compiled_mask=state.compiled_mask,
            page_order=state.page_order,
            stale=False,
            error_code=error_code,
        )

    def marked_stale(self) -> PageOrderObservation:
        return PageOrderObservation(
            compiled_mask=self.compiled_mask,
            page_order=self.page_order,
            stale=True,
            error_code=None,
        )


def page_order_actions_for_manifest(manifest_action: Any) -> tuple[str, str]:
    """Derive sibling page-order actions from the selected manifest action."""
    if not isinstance(manifest_action, str):
        raise PageOrderTransportUnavailableError("Manifest action must be a string")

    domain, separator, service = manifest_action.partition(".")
    if (
        separator != "."
        or domain != ESPHOME_DOMAIN
        or not service.endswith(MANIFEST_ACTION_SUFFIX)
    ):
        raise PageOrderTransportUnavailableError(
            f"Invalid ESPHome manifest action: {manifest_action}"
        )

    prefix = service[: -len(MANIFEST_ACTION_SUFFIX)]
    if not prefix:
        raise PageOrderTransportUnavailableError(
            f"Invalid ESPHome manifest action: {manifest_action}"
        )

    return (
        f"{ESPHOME_DOMAIN}.{prefix}{PAGE_ORDER_READ_ACTION_SUFFIX}",
        f"{ESPHOME_DOMAIN}.{prefix}{PAGE_ORDER_WRITE_ACTION_SUFFIX}",
    )


def _split_action(action: str, suffix: str) -> tuple[str, str]:
    domain, separator, service = action.partition(".")
    if separator != "." or domain != ESPHOME_DOMAIN or not service.endswith(suffix):
        raise PageOrderTransportUnavailableError(
            f"Invalid ESPHome page-order action: {action}"
        )
    return domain, service


def _action_available(hass: HomeAssistant, action: str) -> bool:
    domain, separator, service_name = action.partition(".")
    if separator != ".":
        return False
    service = hass.services.async_services().get(domain, {}).get(service_name)
    return service is not None and service.supports_response is SupportsResponse.ONLY


def page_order_actions_available(hass: HomeAssistant, manifest_action: Any) -> bool:
    """Return whether both dedicated sibling actions are currently usable."""
    try:
        read_action, write_action = page_order_actions_for_manifest(manifest_action)
    except PageOrderTransportUnavailableError:
        return False
    return _action_available(hass, read_action) and _action_available(hass, write_action)


def _validated_compiled_mask(response: dict[str, Any]) -> int:
    compiled_mask = response.get("compiled_mask")
    if (
        isinstance(compiled_mask, bool)
        or not isinstance(compiled_mask, int)
        or compiled_mask <= 0
        or compiled_mask > PAGE_ORDER_FUNCTIONAL_BITS
    ):
        raise PageOrderTransportResponseError(
            "Page-order response has invalid compiled_mask"
        )
    return compiled_mask


def _compiled_slots(compiled_mask: int) -> tuple[int, ...]:
    return tuple(
        slot
        for slot in range(1, 7)
        if compiled_mask & (1 << (slot - 1))
    )


def _validated_page_order(
    response: dict[str, Any],
    compiled_mask: int,
) -> tuple[int, ...]:
    value = response.get("page_order")
    if not isinstance(value, list):
        raise PageOrderTransportResponseError(
            "Page-order response has invalid page_order"
        )

    order: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 6:
            raise PageOrderTransportResponseError(
                "Page-order response contains invalid slot"
            )
        order.append(item)

    expected = _compiled_slots(compiled_mask)
    if len(order) != len(expected) or len(set(order)) != len(order):
        raise PageOrderTransportResponseError(
            "Page-order response is not a complete permutation"
        )
    if set(order) != set(expected):
        raise PageOrderTransportResponseError(
            "Page-order response does not match compiled slots"
        )
    return tuple(order)


def _validated_requested_order(page_order: Sequence[int]) -> tuple[int, ...]:
    if isinstance(page_order, (str, bytes)) or not isinstance(page_order, Sequence):
        raise PageOrderTransportResponseError(
            "Requested page_order must be a slot sequence"
        )

    order: list[int] = []
    for item in page_order:
        if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 6:
            raise PageOrderTransportResponseError(
                "Requested page_order contains invalid slot"
            )
        order.append(item)
    if not order or len(set(order)) != len(order):
        raise PageOrderTransportResponseError(
            "Requested page_order must contain unique functional slots"
        )
    return tuple(order)


async def _async_call_action(
    hass: HomeAssistant,
    action: str,
    data: dict[str, Any],
    *,
    suffix: str,
) -> dict[str, Any]:
    domain, service = _split_action(action, suffix)
    if not _action_available(hass, action):
        raise PageOrderTransportUnavailableError(
            f"ESPHome page-order action unavailable: {action}"
        )

    try:
        async with asyncio.timeout(PAGE_ORDER_RESPONSE_TIMEOUT_SECONDS):
            response = await hass.services.async_call(
                domain,
                service,
                data,
                blocking=True,
                return_response=True,
            )
    except TimeoutError as err:
        raise PageOrderTransportUnavailableError(
            f"Timed out calling ESPHome page-order action: {action}"
        ) from err
    except HomeAssistantError as err:
        raise PageOrderTransportUnavailableError(
            f"ESPHome page-order action failed: {action}"
        ) from err

    if not isinstance(response, dict):
        raise PageOrderTransportResponseError(
            "ESPHome page-order action returned no JSON object"
        )
    return response


async def async_fetch_page_order(
    hass: HomeAssistant,
    manifest_action: str,
) -> PageOrderState:
    """Read and validate the device-authoritative page order."""
    read_action, _ = page_order_actions_for_manifest(manifest_action)
    response = await _async_call_action(
        hass,
        read_action,
        {},
        suffix=PAGE_ORDER_READ_ACTION_SUFFIX,
    )
    compiled_mask = _validated_compiled_mask(response)
    page_order = _validated_page_order(response, compiled_mask)
    if response.get("error_code") != PAGE_ORDER_OK:
        raise PageOrderTransportResponseError(
            "ESPHome page-order read did not return OK"
        )
    return PageOrderState(
        compiled_mask=compiled_mask,
        page_order=page_order,
    )


async def async_set_page_order(
    hass: HomeAssistant,
    manifest_action: str,
    page_order: Sequence[int],
) -> PageOrderWriteResult:
    """Write one complete desired permutation and validate ACK + readback."""
    requested = _validated_requested_order(page_order)
    _, write_action = page_order_actions_for_manifest(manifest_action)
    response = await _async_call_action(
        hass,
        write_action,
        {"page_order": list(requested)},
        suffix=PAGE_ORDER_WRITE_ACTION_SUFFIX,
    )

    compiled_mask = _validated_compiled_mask(response)
    returned_order = _validated_page_order(response, compiled_mask)
    changed = response.get("changed")
    error_code = response.get("error_code")
    if not isinstance(changed, bool):
        raise PageOrderTransportResponseError(
            "Page-order write response has invalid changed"
        )
    if not isinstance(error_code, str) or not error_code:
        raise PageOrderTransportResponseError(
            "Page-order write response has invalid error_code"
        )
    if error_code != PAGE_ORDER_OK and changed:
        raise PageOrderTransportResponseError(
            "Failed page-order write cannot report changed=true"
        )
    if error_code == PAGE_ORDER_OK and returned_order != requested:
        raise PageOrderTransportResponseError(
            "Successful page-order write returned a different permutation"
        )

    return PageOrderWriteResult(
        compiled_mask=compiled_mask,
        page_order=returned_order,
        changed=changed,
        error_code=error_code,
    )
