"""Home Assistant service handling for MaTouch device commands."""

from __future__ import annotations

from collections.abc import Mapping
import logging
import math
from typing import Any

import voluptuous as vol

from homeassistant.components.climate.const import (
    ATTR_FAN_MODE,
    ATTR_FAN_MODES,
    ATTR_HVAC_MODE,
    ATTR_HVAC_MODES,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ClimateEntityFeature,
    ClimateEntityStateAttribute,
    HVACMode,
    SERVICE_SET_FAN_MODE,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.components.humidifier.const import (
    ATTR_AVAILABLE_MODES,
    ATTR_HUMIDITY,
    ATTR_MAX_HUMIDITY,
    ATTR_MIN_HUMIDITY,
    DEFAULT_MAX_HUMIDITY,
    DEFAULT_MIN_HUMIDITY,
    HumidifierEntityFeature,
    HumidifierEntityStateAttribute,
    SERVICE_SET_HUMIDITY,
    SERVICE_SET_MODE,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.helpers import config_validation as cv

from .auxiliary import auxiliary_value_supported
from .command import (
    ERROR_INVALID_PAYLOAD,
    CommandError,
    MappedCommand,
    RequestDeduplicator,
    parse_mapped_command,
)
from .const import (
    CONF_CONTROL_SLOT,
    CONF_DEVICE_UID,
    CONF_PAGE_SLOT,
)
from .manifest_model import ControlManifest, RuntimeManifest
from .mapping import control_for_slots, effective_entry_data
from .sync import DisplaySyncError, EntryDisplaySync, scene_launcher_availability

SERVICE_COMMAND = "command"

ERROR_ACTION_DENIED = "E_ACTION_DENIED"
ERROR_ACTION_FAILED = "E_ACTION_FAILED"
ERROR_ENTITY_MISSING = "E_ENTITY_MISSING"
ERROR_ENTITY_UNAVAILABLE = "E_ENTITY_UNAVAILABLE"
ERROR_NOT_CONFIGURED = "E_NOT_CONFIGURED"

_LOGGER = logging.getLogger(__name__)


def _service_page_slot(value: Any) -> int:
    """Normalize a page slot sent as a string by ESPHome."""
    if isinstance(value, str) and value.isdigit():
        value = int(value)

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 6
    ):
        raise vol.Invalid("expected integer in range 1..6")
    return value


def _service_control_slot(value: Any) -> int:
    """Normalize a template-defined positive control slot."""
    if isinstance(value, str) and value.isdigit():
        value = int(value)

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise vol.Invalid("expected positive integer")
    return value


def _service_value_number(value: Any) -> float:
    """Accept only finite real numeric service payloads."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise vol.Invalid("expected numeric value")
    number = float(value)
    if not math.isfinite(number):
        raise vol.Invalid("expected finite numeric value")
    return number


COMMAND_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_UID): cv.string,
        vol.Required("protocol_version"): cv.string,
        vol.Required("boot_id"): cv.string,
        vol.Required("request_id"): cv.string,
        vol.Required("command"): cv.string,
        vol.Required(CONF_PAGE_SLOT): _service_page_slot,
        vol.Required(CONF_CONTROL_SLOT): _service_control_slot,
        vol.Optional("value_key"): cv.string,
        vol.Optional("value_number"): _service_value_number,
        vol.Optional("value_text"): cv.string,
    }
)


def _validation_error(error: CommandError) -> ServiceValidationError:
    return ServiceValidationError(f"{error.code}: {error}")


def _entity_state_or_error(hass: HomeAssistant, entity_id: str):
    state = hass.states.get(entity_id)
    if state is None:
        raise ServiceValidationError(
            f"{ERROR_ENTITY_MISSING}: Entity does not exist: {entity_id}"
        )
    if state.state in {"unknown", "unavailable"}:
        raise ServiceValidationError(
            f"{ERROR_ENTITY_UNAVAILABLE}: Entity state is {state.state}: {entity_id}"
        )
    return state


_CLIMATE_SELECTABLE_HVAC_MODES = frozenset(
    {
        HVACMode.OFF.value,
        HVACMode.HEAT.value,
        HVACMode.COOL.value,
        HVACMode.AUTO.value,
        HVACMode.DRY.value,
        HVACMode.FAN_ONLY.value,
    }
)
_CLIMATE_SINGLE_SETPOINT_HVAC_MODES = frozenset(
    {
        HVACMode.HEAT.value,
        HVACMode.COOL.value,
        HVACMode.AUTO.value,
        HVACMode.DRY.value,
        HVACMode.FAN_ONLY.value,
    }
)


def _normalized_text(value: Any) -> str | None:
    raw = getattr(value, "value", value)
    if not isinstance(raw, str) or not raw:
        return None
    return raw


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _climate_feature_supported(state: Any, feature: ClimateEntityFeature) -> bool:
    raw = getattr(state, "attributes", {}).get("supported_features", 0)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return False
    return bool(raw & int(feature))


def _climate_text_list(state: Any, attribute: str) -> tuple[str, ...] | None:
    raw_values = getattr(state, "attributes", {}).get(attribute)
    if not isinstance(raw_values, (list, tuple)) or not raw_values:
        return None

    values: list[str] = []
    for raw_value in raw_values:
        value = _normalized_text(raw_value)
        if value is None:
            return None
        values.append(value)
    return tuple(values)


def _climate_hvac_action(
    command: MappedCommand,
    state: Any,
) -> tuple[str, str, dict[str, Any]]:
    requested = command.value_text
    if requested is None or requested not in _CLIMATE_SELECTABLE_HVAC_MODES:
        raise ServiceValidationError(
            f"{ERROR_INVALID_PAYLOAD}: Unsupported Climate hvac_mode selection"
        )

    modes = _climate_text_list(state, ATTR_HVAC_MODES)
    if modes is None or requested not in modes:
        raise ServiceValidationError(
            f"{ERROR_INVALID_PAYLOAD}: hvac_mode is not currently supported by "
            f"{command.entity_id}"
        )

    return (
        "climate",
        SERVICE_SET_HVAC_MODE,
        {
            ATTR_ENTITY_ID: command.entity_id,
            ATTR_HVAC_MODE: requested,
        },
    )


def _climate_temperature_action(
    command: MappedCommand,
    state: Any,
) -> tuple[str, str, dict[str, Any]]:
    if not _climate_feature_supported(
        state,
        ClimateEntityFeature.TARGET_TEMPERATURE,
    ):
        raise ServiceValidationError(
            f"{ERROR_INVALID_PAYLOAD}: target_temperature is not currently "
            f"supported by {command.entity_id}"
        )

    current_mode = _normalized_text(getattr(state, "state", None))
    if current_mode not in _CLIMATE_SINGLE_SETPOINT_HVAC_MODES:
        raise ServiceValidationError(
            f"{ERROR_INVALID_PAYLOAD}: target_temperature is inactive or "
            f"unsupported in hvac_mode {current_mode}"
        )

    attributes = getattr(state, "attributes", {})
    current_target = _finite_number(
        attributes.get(ClimateEntityStateAttribute.TARGET_TEMPERATURE.value)
    )
    minimum = _finite_number(attributes.get(ATTR_MIN_TEMP))
    maximum = _finite_number(attributes.get(ATTR_MAX_TEMP))
    requested = command.value_number
    if (
        current_target is None
        or minimum is None
        or maximum is None
        or requested is None
        or minimum > maximum
        or not minimum <= current_target <= maximum
        or not minimum <= requested <= maximum
    ):
        raise ServiceValidationError(
            f"{ERROR_INVALID_PAYLOAD}: target_temperature capability data is "
            f"invalid for {command.entity_id}"
        )

    return (
        "climate",
        SERVICE_SET_TEMPERATURE,
        {
            ATTR_ENTITY_ID: command.entity_id,
            ClimateEntityStateAttribute.TARGET_TEMPERATURE.value: requested,
        },
    )


def _climate_fan_action(
    command: MappedCommand,
    state: Any,
) -> tuple[str, str, dict[str, Any]]:
    if _normalized_text(getattr(state, "state", None)) == HVACMode.OFF.value:
        raise ServiceValidationError(
            f"{ERROR_INVALID_PAYLOAD}: fan_mode is inactive while Climate is off"
        )
    if not _climate_feature_supported(state, ClimateEntityFeature.FAN_MODE):
        raise ServiceValidationError(
            f"{ERROR_INVALID_PAYLOAD}: fan_mode is not currently supported by "
            f"{command.entity_id}"
        )

    modes = _climate_text_list(state, ATTR_FAN_MODES)
    current = _normalized_text(getattr(state, "attributes", {}).get(ATTR_FAN_MODE))
    if (
        modes is None
        or current is None
        or len(set(modes)) != len(modes)
        or modes.count(current) != 1
    ):
        raise ServiceValidationError(
            f"{ERROR_INVALID_PAYLOAD}: fan_mode capability data is invalid for "
            f"{command.entity_id}"
        )

    next_mode = modes[(modes.index(current) + 1) % len(modes)]
    return (
        "climate",
        SERVICE_SET_FAN_MODE,
        {
            ATTR_ENTITY_ID: command.entity_id,
            ATTR_FAN_MODE: next_mode,
        },
    )


def _humidifier_feature_supported(
    state: Any,
    feature: HumidifierEntityFeature,
) -> bool:
    raw = getattr(state, "attributes", {}).get("supported_features", 0)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return False
    return bool(raw & int(feature))


def _humidifier_mode_list(state: Any) -> tuple[str, ...] | None:
    raw_modes = getattr(state, "attributes", {}).get(ATTR_AVAILABLE_MODES)
    if not isinstance(raw_modes, (list, tuple)) or not 1 <= len(raw_modes) <= 6:
        return None

    modes: list[str] = []
    for raw_mode in raw_modes:
        mode = _normalized_text(raw_mode)
        if mode is None or len(mode.encode("utf-8")) > 48:
            return None
        modes.append(mode)
    return tuple(modes)


def _humidifier_mode_action(
    command: MappedCommand,
    state: Any,
) -> tuple[str, str, dict[str, Any]]:
    if getattr(state, "state", None) != STATE_ON:
        raise ServiceValidationError(
            f"{ERROR_INVALID_PAYLOAD}: mode is inactive while Dehumidifier is off"
        )
    if not _humidifier_feature_supported(
        state,
        HumidifierEntityFeature.MODES,
    ):
        raise ServiceValidationError(
            f"{ERROR_INVALID_PAYLOAD}: mode is not currently supported by "
            f"{command.entity_id}"
        )

    requested = command.value_text
    modes = _humidifier_mode_list(state)
    current = _normalized_text(
        getattr(state, "attributes", {}).get(
            HumidifierEntityStateAttribute.MODE.value
        )
    )
    if (
        requested is None
        or modes is None
        or current is None
        or current not in modes
        or requested not in modes
    ):
        raise ServiceValidationError(
            f"{ERROR_INVALID_PAYLOAD}: mode capability data is invalid or the "
            f"requested mode is not currently supported by {command.entity_id}"
        )

    return (
        "humidifier",
        SERVICE_SET_MODE,
        {
            ATTR_ENTITY_ID: command.entity_id,
            HumidifierEntityStateAttribute.MODE.value: requested,
        },
    )


def _humidifier_target_humidity_action(
    command: MappedCommand,
    state: Any,
) -> tuple[str, str, dict[str, Any]]:
    if getattr(state, "state", None) != STATE_ON:
        raise ServiceValidationError(
            f"{ERROR_INVALID_PAYLOAD}: target_humidity is inactive while "
            "Dehumidifier is off"
        )

    attributes = getattr(state, "attributes", {})
    current_target = _finite_number(attributes.get(ATTR_HUMIDITY))
    minimum = _finite_number(
        attributes.get(ATTR_MIN_HUMIDITY, DEFAULT_MIN_HUMIDITY)
    )
    maximum = _finite_number(
        attributes.get(ATTR_MAX_HUMIDITY, DEFAULT_MAX_HUMIDITY)
    )
    requested = command.value_number
    if (
        current_target is None
        or minimum is None
        or maximum is None
        or requested is None
        or minimum > maximum
        or not minimum <= current_target <= maximum
        or not minimum <= requested <= maximum
    ):
        raise ServiceValidationError(
            f"{ERROR_INVALID_PAYLOAD}: target_humidity capability data is "
            f"invalid for {command.entity_id}"
        )

    return (
        "humidifier",
        SERVICE_SET_HUMIDITY,
        {
            ATTR_ENTITY_ID: command.entity_id,
            ATTR_HUMIDITY: requested,
        },
    )


def _action_for_command(
    hass: HomeAssistant,
    command: MappedCommand,
    control: ControlManifest,
) -> tuple[str, str, dict[str, Any]]:
    """Resolve one validated command into exactly one Home Assistant action."""
    domain = command.entity_id.split(".", 1)[0]

    if command.command == "press" and control.state_kind == "none":
        available, error_code = scene_launcher_availability(
            command.entity_id, hass.states.get(command.entity_id)
        )
        if not available:
            raise ServiceValidationError(
                f"{error_code}: Entity cannot be launched: {command.entity_id}"
            )
        return domain, SERVICE_TURN_ON, {ATTR_ENTITY_ID: command.entity_id}

    state = _entity_state_or_error(hass, command.entity_id)

    if command.command == "toggle":
        if state.state not in (STATE_ON, STATE_OFF):
            raise ServiceValidationError(
                f"{ERROR_ENTITY_UNAVAILABLE}: Entity state is "
                f"{state.state}: {command.entity_id}"
            )
        service = SERVICE_TURN_OFF if state.state == STATE_ON else SERVICE_TURN_ON
        return domain, service, {ATTR_ENTITY_ID: command.entity_id}

    if domain == "climate" and command.command == "turn_on":
        raw_features = state.attributes.get("supported_features")
        if (
            state.state != STATE_OFF
            or isinstance(raw_features, bool)
            or not isinstance(raw_features, int)
            or raw_features < 0
            or not raw_features & int(ClimateEntityFeature.TURN_ON)
        ):
            raise ServiceValidationError(
                f"{ERROR_INVALID_PAYLOAD}: Climate turn_on requires current OFF and TURN_ON"
            )
        return "climate", SERVICE_TURN_ON, {ATTR_ENTITY_ID: command.entity_id}

    if (
        domain == "climate"
        and command.command == "select_option"
        and command.value_text is not None
    ):
        return _climate_hvac_action(command, state)

    if (
        domain == "climate"
        and command.command == "set_value"
        and command.value_key == "target_temperature"
        and command.value_number is not None
    ):
        return _climate_temperature_action(command, state)

    if (
        domain == "climate"
        and command.command == "increment"
        and command.value_key == "fan_mode"
    ):
        return _climate_fan_action(command, state)

    if (
        domain == "humidifier"
        and command.command == "select_option"
        and command.value_key == "mode"
        and command.value_text is not None
    ):
        return _humidifier_mode_action(command, state)

    if (
        domain == "humidifier"
        and command.command == "set_value"
        and command.value_key == "target_humidity"
        and command.value_number is not None
    ):
        return _humidifier_target_humidity_action(command, state)

    if (
        command.command == "set_value"
        and command.value_key == "brightness"
        and command.value_number is not None
    ):
        if not auxiliary_value_supported(
            control,
            command.entity_id,
            state,
            command.value_key,
        ):
            raise ServiceValidationError(
                f"{ERROR_INVALID_PAYLOAD}: Auxiliary value brightness is not "
                f"supported by {command.entity_id}"
            )
        return (
            "light",
            SERVICE_TURN_ON,
            {
                ATTR_ENTITY_ID: command.entity_id,
                "brightness_pct": command.value_number,
            },
        )

    raise ServiceValidationError(
        f"{ERROR_INVALID_PAYLOAD}: Unsupported command {command.command}"
    )


async def async_execute_mapped_command(
    hass: HomeAssistant,
    command: MappedCommand,
    deduplicator: RequestDeduplicator,
    *,
    context: Context,
    control: ControlManifest,
    sync: EntryDisplaySync | None = None,
) -> bool:
    """Execute one validated command while preserving caller permissions."""
    if not deduplicator.accept(command.device_uid, command.request_id):
        _LOGGER.info(
            "Ignoring duplicate MaTouch request %s for %s",
            command.request_id,
            command.device_uid,
        )
        return False

    domain, service, service_data = _action_for_command(hass, command, control)

    _LOGGER.info(
        "Executing MaTouch request %s: %s -> %s.%s for %s",
        command.request_id,
        command.diagnostic_address,
        domain,
        service,
        command.entity_id,
    )

    if sync is not None and control.state_kind != "none":
        sync.register_command_correlation(
            context_id=context.id,
            entity_id=command.entity_id,
            request_id=command.request_id,
        )

    try:
        await hass.services.async_call(
            domain,
            service,
            service_data,
            blocking=True,
            context=context,
        )
    except Unauthorized as err:
        if sync is not None:
            sync.clear_command_correlation(
                context_id=context.id,
                request_id=command.request_id,
            )
        raise HomeAssistantError(
            f"{ERROR_ACTION_DENIED}: Permission denied for "
            f"{command.entity_id}"
        ) from err
    except HomeAssistantError as err:
        if sync is not None:
            sync.clear_command_correlation(
                context_id=context.id,
                request_id=command.request_id,
            )
        raise HomeAssistantError(
            f"{ERROR_ACTION_FAILED}: Failed to execute {service} for "
            f"{command.entity_id}"
        ) from err

    _LOGGER.info(
        "Completed MaTouch request %s for %s",
        command.request_id,
        command.entity_id,
    )
    return True


async def async_handle_command(
    hass: HomeAssistant,
    call: ServiceCall,
    entries: Mapping[str, ConfigEntry],
    manifests: Mapping[str, RuntimeManifest],
    deduplicator: RequestDeduplicator,
    syncs: Mapping[str, EntryDisplaySync] | None = None,
) -> None:
    """Resolve a device command to active manifest, mapping and entity."""
    device_uid = call.data.get(CONF_DEVICE_UID)
    if not isinstance(device_uid, str) or not device_uid:
        raise ServiceValidationError(
            f"{ERROR_INVALID_PAYLOAD}: Invalid or missing device_uid"
        )

    entry = entries.get(device_uid)
    manifest = manifests.get(device_uid)
    sync = syncs.get(device_uid) if syncs is not None else None
    if (
        entry is None
        or manifest is None
        or (syncs is not None and sync is None)
    ):
        raise ServiceValidationError(
            f"{ERROR_NOT_CONFIGURED}: No validated runtime configuration for "
            f"{device_uid}"
        )

    if sync is not None:
        try:
            sync.require_command_ready(call.data.get("boot_id"))
        except DisplaySyncError as err:
            raise ServiceValidationError(
                f"{ERROR_NOT_CONFIGURED}: {err}"
            ) from err

    try:
        command = parse_mapped_command(
            effective_entry_data(entry),
            manifest,
            call.data,
        )
    except CommandError as err:
        raise _validation_error(err) from err

    control = control_for_slots(
        manifest,
        command.page_slot,
        command.control_slot,
    )
    if control is None:
        raise ServiceValidationError(
            f"{ERROR_NOT_CONFIGURED}: Control missing from active manifest"
        )

    _LOGGER.info(
        "Accepted MaTouch request %s for %s mapped to %s",
        command.request_id,
        command.diagnostic_address,
        command.entity_id,
    )

    await async_execute_mapped_command(
        hass,
        command,
        deduplicator,
        context=call.context,
        control=control,
        sync=sync,
    )
