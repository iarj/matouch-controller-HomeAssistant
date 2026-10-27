"""ESPHome transport for MaTouch runtime manifests."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError

from .manifest_model import (
    ManifestError,
    RuntimeManifest,
    parse_runtime_manifest,
    validate_runtime_manifest_templates,
)


ESPHOME_DOMAIN = "esphome"
MANIFEST_ACTION_SUFFIX = "_matouch_get_manifest"
MANIFEST_RESPONSE_TIMEOUT_SECONDS = 10
REQUEST_HELLO_ACTION_SUFFIX = "_matouch_request_hello"


class ManifestTransportError(HomeAssistantError):
    """Base error for runtime manifest transport failures."""


class ManifestTransportUnavailableError(ManifestTransportError):
    """Raised when the selected ESPHome manifest action is unavailable."""


class ManifestTransportResponseError(ManifestTransportError):
    """Raised when an ESPHome manifest response is missing or invalid."""


def available_manifest_actions(hass: HomeAssistant) -> tuple[str, ...]:
    """Return ESPHome manifest query actions with mandatory responses."""
    services = hass.services.async_services().get(ESPHOME_DOMAIN, {})

    actions = (
        f"{ESPHOME_DOMAIN}.{service_name}"
        for service_name, service in services.items()
        if service_name.endswith(MANIFEST_ACTION_SUFFIX)
        and service.supports_response is SupportsResponse.ONLY
    )
    return tuple(sorted(actions))


def _split_manifest_action(action: Any) -> tuple[str, str]:
    if not isinstance(action, str):
        raise ManifestTransportUnavailableError(
            "Manifest action must be a string"
        )

    domain, separator, service = action.partition(".")
    if (
        separator != "."
        or domain != ESPHOME_DOMAIN
        or not service.endswith(MANIFEST_ACTION_SUFFIX)
    ):
        raise ManifestTransportUnavailableError(
            f"Invalid ESPHome manifest action: {action}"
        )

    return domain, service


async def async_request_hello(hass: HomeAssistant, manifest_action: str) -> None:
    """Request one existing firmware hello cycle; never supply a boot identity."""
    domain, manifest_service = _split_manifest_action(manifest_action)
    service = (
        manifest_service.removesuffix(MANIFEST_ACTION_SUFFIX)
        + REQUEST_HELLO_ACTION_SUFFIX
    )
    if service not in hass.services.async_services().get(domain, {}):
        raise ManifestTransportUnavailableError(
            f"ESPHome hello action unavailable: {domain}.{service}"
        )
    try:
        async with asyncio.timeout(MANIFEST_RESPONSE_TIMEOUT_SECONDS):
            await hass.services.async_call(domain, service, {}, blocking=True)
    except (TimeoutError, HomeAssistantError) as err:
        raise ManifestTransportUnavailableError(
            f"ESPHome hello action failed: {domain}.{service}"
        ) from err


async def async_fetch_runtime_manifest(
    hass: HomeAssistant,
    action: str,
) -> RuntimeManifest:
    """Fetch, parse and validate a runtime manifest through ESPHome."""
    domain, service = _split_manifest_action(action)

    if action not in available_manifest_actions(hass):
        raise ManifestTransportUnavailableError(
            f"ESPHome manifest action unavailable: {action}"
        )

    try:
        async with asyncio.timeout(MANIFEST_RESPONSE_TIMEOUT_SECONDS):
            response = await hass.services.async_call(
                domain,
                service,
                {},
                blocking=True,
                return_response=True,
            )
    except TimeoutError as err:
        raise ManifestTransportUnavailableError(
            f"Timed out calling ESPHome manifest action: {action}"
        ) from err
    except HomeAssistantError as err:
        raise ManifestTransportUnavailableError(
            f"ESPHome manifest action failed: {action}"
        ) from err

    if not isinstance(response, dict):
        raise ManifestTransportResponseError(
            "ESPHome manifest action returned no JSON object"
        )

    try:
        return parse_runtime_manifest(response)
    except ManifestError as err:
        raise ManifestTransportResponseError(
            "ESPHome manifest action returned an invalid manifest"
        ) from err


async def async_find_manifest_actions_for_device(
    hass: HomeAssistant,
    device_uid: str,
    *,
    exclude: str | None = None,
) -> list[tuple[str, RuntimeManifest]]:
    """Run one bounded resolution pass for one device identity (HA §9.8).

    Each available manifest action with a mandatory response is called at most
    once, sequentially, with the existing manifest timeout. A candidate matches
    only with a valid manifest for the same ``device_uid``. The pass stops at
    the second match: two matches already make the result ambiguous. Manifest
    queries are read-only, so no other unit is modified.
    """
    matches: list[tuple[str, RuntimeManifest]] = []
    for action in available_manifest_actions(hass):
        if action == exclude:
            continue
        try:
            manifest = await async_fetch_runtime_manifest(hass, action)
        except ManifestTransportError:
            continue
        if manifest.device_uid != device_uid:
            continue
        try:
            validate_runtime_manifest_templates(manifest)
        except ManifestError:
            continue
        matches.append((action, manifest))
        if len(matches) > 1:
            break
    return matches
