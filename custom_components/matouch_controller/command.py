"""Validation and routing for commands received from MaTouch devices."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import math
import re
import time
from typing import Any

from homeassistant.const import CONF_ENTITY_ID

from .const import (
    CONF_CONTROL_SLOT,
    CONF_DEVICE_UID,
    CONF_MAPPINGS,
    CONF_PAGE_SLOT,
    CONF_VALIDATED_CONFIG_HASH,
    PROTOCOL_VERSION,
)
from .manifest_model import AuxiliaryValueManifest, ControlManifest, RuntimeManifest
from .mapping import control_for_slots, mapping_for_slots

ERROR_INVALID_PAYLOAD = "E_INVALID_PAYLOAD"
ERROR_NOT_CONFIGURED = "E_NOT_CONFIGURED"
ERROR_PROTOCOL_INCOMPATIBLE = "E_PROTOCOL_INCOMPATIBLE"
ERROR_UNMAPPED_CONTROL = "E_UNMAPPED_CONTROL"

_BOOT_ID_PATTERN = re.compile(r"^[0-9a-f]{8}$")
_REQUEST_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9]{1,10}$")
_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+$")
_PAGE_SLOT_MINIMUM = 1
_PAGE_SLOT_MAXIMUM = 6
_REQUEST_DEDUPLICATION_SECONDS = 60.0


class CommandError(ValueError):
    """Raised when a device command cannot be accepted."""

    def __init__(self, code: str, message: str) -> None:
        """Initialize a command validation error."""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MappedCommand:
    """Validated command routed to one Home Assistant entity."""

    device_uid: str
    boot_id: str
    request_id: str
    command: str
    page_slot: int
    control_slot: int
    entity_id: str
    value_key: str = ""
    value_number: float | None = None
    value_text: str | None = None

    @property
    def diagnostic_address(self) -> str:
        """Return a human-readable address with no identity role."""
        return (
            f"page_slot={self.page_slot},control_slot={self.control_slot}"
        )


class RequestDeduplicator:
    """Remember accepted request IDs for the protocol-defined interval."""

    def __init__(
        self,
        *,
        ttl_seconds: float = _REQUEST_DEDUPLICATION_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize the request cache."""
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

        self._ttl_seconds = ttl_seconds
        self._monotonic = monotonic
        self._expires_at: dict[tuple[str, str], float] = {}

    def accept(self, device_uid: str, request_id: str) -> bool:
        """Return false for a duplicate request still inside the TTL."""
        now = self._monotonic()
        self._expires_at = {
            key: expires_at
            for key, expires_at in self._expires_at.items()
            if expires_at > now
        }

        key = (device_uid, request_id)
        if key in self._expires_at:
            return False

        self._expires_at[key] = now + self._ttl_seconds
        return True


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise CommandError(
            ERROR_INVALID_PAYLOAD,
            f"Invalid or missing {key}",
        )
    return value


def _page_slot(payload: Mapping[str, Any]) -> int:
    value = payload.get(CONF_PAGE_SLOT)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not _PAGE_SLOT_MINIMUM <= value <= _PAGE_SLOT_MAXIMUM
    ):
        raise CommandError(
            ERROR_INVALID_PAYLOAD,
            f"Invalid or missing {CONF_PAGE_SLOT}",
        )
    return value


def _control_slot(payload: Mapping[str, Any]) -> int:
    value = payload.get(CONF_CONTROL_SLOT)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CommandError(
            ERROR_INVALID_PAYLOAD,
            f"Invalid or missing {CONF_CONTROL_SLOT}",
        )
    return value


def _auxiliary_value(
    control: ControlManifest,
    payload: Mapping[str, Any],
) -> tuple[str, float | None, str | None]:
    value_key = _required_string(payload, "value_key")
    auxiliary = next(
        (
            value
            for value in control.auxiliary_values
            if value.key == value_key
        ),
        None,
    )
    if auxiliary is None:
        raise CommandError(
            ERROR_INVALID_PAYLOAD,
            f"Auxiliary value is not declared by active manifest: {value_key}",
        )
    return _typed_auxiliary_value(auxiliary, payload)


def _typed_auxiliary_value(
    auxiliary: AuxiliaryValueManifest,
    payload: Mapping[str, Any],
) -> tuple[str, float | None, str | None]:
    if auxiliary.kind == "number":
        raw = payload.get("value_number")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise CommandError(
                ERROR_INVALID_PAYLOAD,
                f"Invalid or missing value_number for {auxiliary.key}",
            )
        value_number = float(raw)
        if not math.isfinite(value_number):
            raise CommandError(
                ERROR_INVALID_PAYLOAD,
                f"value_number must be finite for {auxiliary.key}",
            )
        if auxiliary.key == "brightness" and not 0.0 <= value_number <= 100.0:
            raise CommandError(
                ERROR_INVALID_PAYLOAD,
                "Brightness value_number must be in range 0..100",
            )
        return auxiliary.key, value_number, None

    if auxiliary.kind in {"text", "option"}:
        raw = payload.get("value_text")
        if not isinstance(raw, str):
            raise CommandError(
                ERROR_INVALID_PAYLOAD,
                f"Invalid or missing value_text for {auxiliary.key}",
            )
        return auxiliary.key, None, raw

    raise CommandError(
        ERROR_INVALID_PAYLOAD,
        f"Unsupported auxiliary kind for command: {auxiliary.kind}",
    )


def parse_mapped_command(
    entry_data: Mapping[str, Any],
    manifest: RuntimeManifest,
    payload: Mapping[str, Any],
) -> MappedCommand:
    """Validate a control command against active manifest and mapping."""
    configured_device_uid = entry_data.get(CONF_DEVICE_UID)
    device_uid = _required_string(payload, CONF_DEVICE_UID)
    if device_uid != configured_device_uid or manifest.device_uid != device_uid:
        raise CommandError(
            ERROR_NOT_CONFIGURED,
            "Command device does not match active runtime configuration",
        )

    protocol_version = _required_string(payload, "protocol_version")
    if _VERSION_PATTERN.fullmatch(protocol_version) is None:
        raise CommandError(
            ERROR_INVALID_PAYLOAD,
            f"Invalid protocol version: {protocol_version}",
        )
    if protocol_version.split(".", 1)[0] != PROTOCOL_VERSION.split(".", 1)[0]:
        raise CommandError(
            ERROR_PROTOCOL_INCOMPATIBLE,
            f"Unsupported protocol version: {protocol_version}",
        )

    validated_hash = entry_data.get(CONF_VALIDATED_CONFIG_HASH)
    if validated_hash != manifest.config_hash:
        raise CommandError(
            ERROR_NOT_CONFIGURED,
            "Persistent mapping is not validated for the active config_hash",
        )

    boot_id = _required_string(payload, "boot_id")
    if _BOOT_ID_PATTERN.fullmatch(boot_id) is None:
        raise CommandError(ERROR_INVALID_PAYLOAD, "Invalid boot_id")

    request_id = _required_string(payload, "request_id")
    if (
        _REQUEST_ID_PATTERN.fullmatch(request_id) is None
        or not request_id.startswith(f"{boot_id}-")
    ):
        raise CommandError(ERROR_INVALID_PAYLOAD, "Invalid request_id")

    command = _required_string(payload, "command")
    page_slot = _page_slot(payload)
    control_slot = _control_slot(payload)

    mapping = mapping_for_slots(
        entry_data.get(CONF_MAPPINGS),
        page_slot,
        control_slot,
    )
    if mapping is None:
        raise CommandError(
            ERROR_UNMAPPED_CONTROL,
            "Control is not mapped: "
            f"page_slot={page_slot},control_slot={control_slot}",
        )

    control = control_for_slots(manifest, page_slot, control_slot)
    if control is None:
        raise CommandError(
            ERROR_NOT_CONFIGURED,
            "No active manifest capability for "
            f"page_slot={page_slot},control_slot={control_slot}",
        )

    page = next(
        (page for page in manifest.pages if page.page_slot == page_slot),
        None,
    )

    value_key = ""
    value_number: float | None = None
    value_text: str | None = None
    if command == "set_value":
        value_key, value_number, value_text = _auxiliary_value(control, payload)
        if (
            page is not None
            and page.template == "climate"
            and page.template_version in ("1.0", "2.0")
            and value_key != "target_temperature"
        ):
            raise CommandError(
                ERROR_INVALID_PAYLOAD,
                "Climate set_value requires target_temperature",
            )
        if (
            page is not None
            and page.template == "dehumidifier"
            and page.template_version == "2.0"
            and value_key != "target_humidity"
        ):
            raise CommandError(
                ERROR_INVALID_PAYLOAD,
                "Dehumidifier set_value requires target_humidity",
            )
    elif (
        command == "turn_on"
        and page is not None
        and (page.template, page.template_version) == ("climate", "2.0")
    ):
        if any(key in payload for key in ("value_key", "value_number", "value_text")):
            raise CommandError(
                ERROR_INVALID_PAYLOAD,
                "Climate turn_on does not accept value parameters",
            )
    elif command == control.function:
        if command == "press" and any(
            key in payload for key in ("value_key", "value_number", "value_text")
        ):
            raise CommandError(
                ERROR_INVALID_PAYLOAD,
                "Press does not accept parameters or value_key",
            )
        if (
            command == "select_option"
            and page is not None
            and page.template == "climate"
            and page.template_version in ("1.0", "2.0")
        ):
            value_text = _required_string(payload, "value_text")
    elif (
        command == "select_option"
        and page is not None
        and page.template == "dehumidifier"
        and page.template_version == "2.0"
    ):
        value_key, value_number, value_text = _auxiliary_value(control, payload)
        if value_key != "mode" or not value_text:
            raise CommandError(
                ERROR_INVALID_PAYLOAD,
                "Dehumidifier select_option requires declared mode auxiliary value",
            )
    elif (
        command == "increment"
        and page is not None
        and page.template == "climate"
        and page.template_version in ("1.0", "2.0")
    ):
        value_key = _required_string(payload, "value_key")
        if value_key != "fan_mode" or not any(
            value.key == value_key and value.kind == "text"
            for value in control.auxiliary_values
        ):
            raise CommandError(
                ERROR_INVALID_PAYLOAD,
                "Climate increment requires declared fan_mode auxiliary value",
            )
    else:
        raise CommandError(
            ERROR_INVALID_PAYLOAD,
            "Command does not match active manifest capability: "
            f"page_slot={page_slot},control_slot={control_slot}",
        )

    entity_id = mapping.get(CONF_ENTITY_ID)
    if not isinstance(entity_id, str) or "." not in entity_id:
        raise CommandError(
            ERROR_NOT_CONFIGURED,
            "Control has no valid entity mapping: "
            f"page_slot={page_slot},control_slot={control_slot}",
        )

    domain = entity_id.split(".", 1)[0]
    if domain not in control.allowed_domains:
        raise CommandError(
            ERROR_NOT_CONFIGURED,
            f"Mapped entity domain is not allowed: {domain}",
        )

    return MappedCommand(
        device_uid=device_uid,
        boot_id=boot_id,
        request_id=request_id,
        command=command,
        page_slot=page_slot,
        control_slot=control_slot,
        entity_id=entity_id,
        value_key=value_key,
        value_number=value_number,
        value_text=value_text,
    )
