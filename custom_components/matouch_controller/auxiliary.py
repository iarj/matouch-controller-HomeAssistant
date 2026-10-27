"""Generic auxiliary runtime values for mapped Home Assistant entities."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from homeassistant.components.climate.const import ClimateEntityFeature
from homeassistant.components.humidifier.const import (
    ATTR_ACTION,
    ATTR_AVAILABLE_MODES,
    ATTR_CURRENT_HUMIDITY as ATTR_HUMIDIFIER_CURRENT_HUMIDITY,
    ATTR_HUMIDITY,
    ATTR_MAX_HUMIDITY,
    ATTR_MIN_HUMIDITY,
    ATTR_TARGET_HUMIDITY_STEP,
    DEFAULT_MAX_HUMIDITY,
    DEFAULT_MIN_HUMIDITY,
    HumidifierEntityFeature,
)
from homeassistant.core import State

from .manifest_model import AuxiliaryValueManifest, ControlManifest

_ATTR_BRIGHTNESS = "brightness"
_ATTR_SUPPORTED_COLOR_MODES = "supported_color_modes"
_ATTR_SUPPORTED_FEATURES = "supported_features"

_ATTR_HVAC_MODES = "hvac_modes"
_ATTR_TARGET_TEMPERATURE = "temperature"
_ATTR_CURRENT_TEMPERATURE = "current_temperature"
_ATTR_MIN_TEMP = "min_temp"
_ATTR_MAX_TEMP = "max_temp"
_ATTR_TARGET_TEMP_STEP = "target_temp_step"
_ATTR_TEMPERATURE_UNIT = "temperature_unit"
_ATTR_UNIT_OF_MEASUREMENT = "unit_of_measurement"
_ATTR_FAN_MODE = "fan_mode"
_ATTR_FAN_MODES = "fan_modes"
_ATTR_HVAC_ACTION = "hvac_action"
_ATTR_CURRENT_HUMIDITY = "current_humidity"
_ATTR_TARGET_TEMP_LOW = "target_temp_low"
_ATTR_TARGET_TEMP_HIGH = "target_temp_high"

_CLIMATE_HVAC_MODE_BITS = {
    "off": 0,
    "heat": 1,
    "cool": 2,
    "heat_cool": 3,
    "auto": 4,
    "dry": 5,
    "fan_only": 6,
}

_BRIGHTNESS_CAPABLE_LIGHT_MODES = frozenset(
    {
        "brightness",
        "color_temp",
        "hs",
        "xy",
        "rgb",
        "rgbw",
        "rgbww",
        "white",
    }
)


@dataclass(frozen=True)
class AuxiliaryRuntimeValue:
    """One typed auxiliary value resolved for a mapped HA entity."""

    key: str
    kind: str
    supported: bool
    available: bool
    number: float = 0.0
    text: str = ""


def _normalized_text(value: Any) -> str | None:
    raw = getattr(value, "value", value)
    if not isinstance(raw, str) or not raw:
        return None
    return raw


def _normalized_mode(value: Any) -> str:
    return _normalized_text(value) or ""


def _state_attributes(state: State | None) -> dict[str, Any]:
    attributes = getattr(state, "attributes", None)
    if isinstance(attributes, dict):
        return attributes
    return {}


def _entity_available(state: State | None) -> bool:
    return (
        state is not None
        and isinstance(getattr(state, "state", None), str)
        and state.state not in {"unknown", "unavailable"}
    )


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _supported_features(state: State | None) -> int:
    raw = _state_attributes(state).get(_ATTR_SUPPORTED_FEATURES, 0)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw


def _feature_supported(
    state: State | None,
    feature: ClimateEntityFeature,
) -> bool:
    return bool(_supported_features(state) & int(feature))


def _number_value(
    *,
    key: str,
    supported: bool,
    entity_available: bool,
    raw: Any,
    positive: bool = False,
) -> AuxiliaryRuntimeValue:
    number = _finite_number(raw)
    value_available = (
        supported
        and entity_available
        and number is not None
        and (not positive or number > 0)
    )
    return AuxiliaryRuntimeValue(
        key=key,
        kind="number",
        supported=supported,
        available=value_available,
        number=number if value_available and number is not None else 0.0,
    )


def _text_value(
    *,
    key: str,
    supported: bool,
    entity_available: bool,
    raw: Any,
) -> AuxiliaryRuntimeValue:
    text = _normalized_text(raw)
    value_available = supported and entity_available and text is not None
    return AuxiliaryRuntimeValue(
        key=key,
        kind="text",
        supported=supported,
        available=value_available,
        text=text if value_available and text is not None else "",
    )


def light_supports_brightness(entity_id: str, state: State | None) -> bool:
    """Return whether one concrete light exposes a brightness-capable color mode."""
    if not entity_id.startswith("light.") or state is None:
        return False

    modes = _state_attributes(state).get(_ATTR_SUPPORTED_COLOR_MODES)
    if not isinstance(modes, (list, tuple, set, frozenset)):
        return False

    return any(
        _normalized_mode(mode) in _BRIGHTNESS_CAPABLE_LIGHT_MODES
        for mode in modes
    )


def _brightness_value(entity_id: str, state: State | None) -> AuxiliaryRuntimeValue:
    supported = light_supports_brightness(entity_id, state)
    if not supported or not _entity_available(state):
        return AuxiliaryRuntimeValue(
            key="brightness",
            kind="number",
            supported=supported,
            available=False,
        )

    raw = _state_attributes(state).get(_ATTR_BRIGHTNESS)
    brightness = _finite_number(raw)
    if brightness is None:
        return AuxiliaryRuntimeValue(
            key="brightness",
            kind="number",
            supported=True,
            available=False,
        )

    brightness = min(255.0, max(0.0, brightness))
    return AuxiliaryRuntimeValue(
        key="brightness",
        kind="number",
        supported=True,
        available=True,
        number=brightness * 100.0 / 255.0,
    )


def _climate_hvac_modes_mask(
    state: State | None,
    *,
    entity_available: bool,
) -> AuxiliaryRuntimeValue:
    attributes = _state_attributes(state)
    supported = state is not None and _ATTR_HVAC_MODES in attributes
    raw_modes = attributes.get(_ATTR_HVAC_MODES)
    if (
        not supported
        or not entity_available
        or not isinstance(raw_modes, (list, tuple))
        or not raw_modes
    ):
        return AuxiliaryRuntimeValue(
            key="hvac_modes_mask",
            kind="number",
            supported=supported,
            available=False,
        )

    modes: list[str] = []
    for raw_mode in raw_modes:
        mode = _normalized_text(raw_mode)
        if mode is None:
            return AuxiliaryRuntimeValue(
                key="hvac_modes_mask",
                kind="number",
                supported=True,
                available=False,
            )
        modes.append(mode)

    mask = 0
    for mode in modes:
        bit = _CLIMATE_HVAC_MODE_BITS.get(mode)
        if bit is not None:
            mask |= 1 << bit

    return AuxiliaryRuntimeValue(
        key="hvac_modes_mask",
        kind="number",
        supported=True,
        available=True,
        number=float(mask),
    )


def _climate_fan_modes(state: State | None) -> tuple[str, ...] | None:
    raw_modes = _state_attributes(state).get(_ATTR_FAN_MODES)
    if not isinstance(raw_modes, (list, tuple)) or not raw_modes:
        return None

    modes: list[str] = []
    for raw_mode in raw_modes:
        mode = _normalized_text(raw_mode)
        if mode is None:
            return None
        modes.append(mode)
    return tuple(modes)


def _climate_fan_index(
    state: State | None,
    *,
    supported: bool,
    entity_available: bool,
) -> AuxiliaryRuntimeValue:
    modes = _climate_fan_modes(state)
    current = _normalized_text(_state_attributes(state).get(_ATTR_FAN_MODE))
    if (
        not supported
        or not entity_available
        or modes is None
        or current is None
        or modes.count(current) != 1
    ):
        return AuxiliaryRuntimeValue(
            key="fan_mode_index",
            kind="number",
            supported=supported,
            available=False,
        )

    return AuxiliaryRuntimeValue(
        key="fan_mode_index",
        kind="number",
        supported=True,
        available=True,
        number=float(modes.index(current) + 1),
    )


def _climate_fan_count(
    state: State | None,
    *,
    supported: bool,
    entity_available: bool,
) -> AuxiliaryRuntimeValue:
    modes = _climate_fan_modes(state)
    value_available = supported and entity_available and modes is not None
    return AuxiliaryRuntimeValue(
        key="fan_mode_count",
        kind="number",
        supported=supported,
        available=value_available,
        number=float(len(modes)) if value_available and modes is not None else 0.0,
    )


def _climate_temperature_unit(
    state: State | None,
    *,
    entity_available: bool,
    temperature_unit: str | None,
) -> AuxiliaryRuntimeValue:
    attributes = _state_attributes(state)
    raw_unit: Any = temperature_unit
    if raw_unit is None:
        raw_unit = attributes.get(_ATTR_TEMPERATURE_UNIT)
    if raw_unit is None:
        raw_unit = attributes.get(_ATTR_UNIT_OF_MEASUREMENT)
    unit = _normalized_text(raw_unit)
    supported = state is not None and unit is not None
    return AuxiliaryRuntimeValue(
        key="temperature_unit",
        kind="text",
        supported=supported,
        available=supported and entity_available,
        text=unit if supported and entity_available and unit is not None else "",
    )


def _climate_value(
    auxiliary: AuxiliaryValueManifest,
    state: State | None,
    *,
    temperature_unit: str | None,
) -> AuxiliaryRuntimeValue:
    attributes = _state_attributes(state)
    entity_available = _entity_available(state)
    key = auxiliary.key

    if key == "power_on" and auxiliary.kind == "number":
        raw_features = attributes.get(_ATTR_SUPPORTED_FEATURES)
        supported = (
            isinstance(raw_features, int)
            and not isinstance(raw_features, bool)
            and raw_features >= 0
            and bool(raw_features & int(ClimateEntityFeature.TURN_ON))
        )
        available = (
            supported and entity_available
            and bool(_normalized_text(getattr(state, "state", None)))
        )
        return AuxiliaryRuntimeValue(
            key=key,
            kind="number",
            supported=supported,
            available=available,
            number=1.0 if available else 0.0,
        )

    if key == "hvac_modes_mask" and auxiliary.kind == "number":
        return _climate_hvac_modes_mask(
            state,
            entity_available=entity_available,
        )

    if key == "target_temperature" and auxiliary.kind == "number":
        supported = _feature_supported(
            state,
            ClimateEntityFeature.TARGET_TEMPERATURE,
        )
        return _number_value(
            key=key,
            supported=supported,
            entity_available=entity_available,
            raw=attributes.get(_ATTR_TARGET_TEMPERATURE),
        )

    if key == "current_temperature" and auxiliary.kind == "number":
        supported = state is not None and _ATTR_CURRENT_TEMPERATURE in attributes
        return _number_value(
            key=key,
            supported=supported,
            entity_available=entity_available,
            raw=attributes.get(_ATTR_CURRENT_TEMPERATURE),
        )

    if key == "min_temp" and auxiliary.kind == "number":
        supported = state is not None and _ATTR_MIN_TEMP in attributes
        return _number_value(
            key=key,
            supported=supported,
            entity_available=entity_available,
            raw=attributes.get(_ATTR_MIN_TEMP),
        )

    if key == "max_temp" and auxiliary.kind == "number":
        supported = state is not None and _ATTR_MAX_TEMP in attributes
        return _number_value(
            key=key,
            supported=supported,
            entity_available=entity_available,
            raw=attributes.get(_ATTR_MAX_TEMP),
        )

    if key == "target_temp_step" and auxiliary.kind == "number":
        supported = state is not None and _ATTR_TARGET_TEMP_STEP in attributes
        return _number_value(
            key=key,
            supported=supported,
            entity_available=entity_available,
            raw=attributes.get(_ATTR_TARGET_TEMP_STEP),
            positive=True,
        )

    if key == "temperature_unit" and auxiliary.kind == "text":
        return _climate_temperature_unit(
            state,
            entity_available=entity_available,
            temperature_unit=temperature_unit,
        )

    fan_supported = _feature_supported(state, ClimateEntityFeature.FAN_MODE)
    if key == "fan_mode" and auxiliary.kind == "text":
        return _text_value(
            key=key,
            supported=fan_supported,
            entity_available=entity_available,
            raw=attributes.get(_ATTR_FAN_MODE),
        )

    if key == "fan_mode_index" and auxiliary.kind == "number":
        return _climate_fan_index(
            state,
            supported=fan_supported,
            entity_available=entity_available,
        )

    if key == "fan_mode_count" and auxiliary.kind == "number":
        return _climate_fan_count(
            state,
            supported=fan_supported,
            entity_available=entity_available,
        )

    if key == "hvac_action" and auxiliary.kind == "text":
        supported = state is not None and _ATTR_HVAC_ACTION in attributes
        return _text_value(
            key=key,
            supported=supported,
            entity_available=entity_available,
            raw=attributes.get(_ATTR_HVAC_ACTION),
        )

    if key == "current_humidity" and auxiliary.kind == "number":
        supported = state is not None and _ATTR_CURRENT_HUMIDITY in attributes
        return _number_value(
            key=key,
            supported=supported,
            entity_available=entity_available,
            raw=attributes.get(_ATTR_CURRENT_HUMIDITY),
        )

    range_supported = _feature_supported(
        state,
        ClimateEntityFeature.TARGET_TEMPERATURE_RANGE,
    )
    if key == "target_temp_low" and auxiliary.kind == "number":
        return _number_value(
            key=key,
            supported=range_supported,
            entity_available=entity_available,
            raw=attributes.get(_ATTR_TARGET_TEMP_LOW),
        )

    if key == "target_temp_high" and auxiliary.kind == "number":
        return _number_value(
            key=key,
            supported=range_supported,
            entity_available=entity_available,
            raw=attributes.get(_ATTR_TARGET_TEMP_HIGH),
        )

    return AuxiliaryRuntimeValue(
        key=auxiliary.key,
        kind=auxiliary.kind,
        supported=False,
        available=False,
    )


def _humidifier_feature_supported(
    state: State | None,
    feature: HumidifierEntityFeature,
) -> bool:
    return bool(_supported_features(state) & int(feature))


def _dehumidifier_modes(
    state: State | None,
    *,
    supported: bool,
    entity_available: bool,
) -> tuple[str, ...] | None:
    if not supported or not entity_available:
        return None

    raw_modes = _state_attributes(state).get(ATTR_AVAILABLE_MODES)
    if not isinstance(raw_modes, (list, tuple)) or not 1 <= len(raw_modes) <= 6:
        return None

    modes: list[str] = []
    for raw_mode in raw_modes:
        mode = _normalized_text(raw_mode)
        if mode is None or len(mode.encode("utf-8")) > 48:
            return None
        modes.append(mode)
    return tuple(modes)


def _dehumidifier_value(
    auxiliary: AuxiliaryValueManifest,
    state: State | None,
) -> AuxiliaryRuntimeValue:
    attributes = _state_attributes(state)
    entity_available = _entity_available(state)
    key = auxiliary.key

    if key == "target_humidity" and auxiliary.kind == "number":
        supported = state is not None and ATTR_HUMIDITY in attributes
        return _number_value(
            key=key,
            supported=supported,
            entity_available=entity_available,
            raw=attributes.get(ATTR_HUMIDITY),
        )

    if key == "current_humidity" and auxiliary.kind == "number":
        supported = (
            state is not None
            and ATTR_HUMIDIFIER_CURRENT_HUMIDITY in attributes
        )
        return _number_value(
            key=key,
            supported=supported,
            entity_available=entity_available,
            raw=attributes.get(ATTR_HUMIDIFIER_CURRENT_HUMIDITY),
        )

    if key == "min_humidity" and auxiliary.kind == "number":
        supported = state is not None
        return _number_value(
            key=key,
            supported=supported,
            entity_available=entity_available,
            raw=attributes.get(ATTR_MIN_HUMIDITY, DEFAULT_MIN_HUMIDITY),
        )

    if key == "max_humidity" and auxiliary.kind == "number":
        supported = state is not None
        return _number_value(
            key=key,
            supported=supported,
            entity_available=entity_available,
            raw=attributes.get(ATTR_MAX_HUMIDITY, DEFAULT_MAX_HUMIDITY),
        )

    if key == "target_humidity_step" and auxiliary.kind == "number":
        supported = state is not None and ATTR_TARGET_HUMIDITY_STEP in attributes
        return _number_value(
            key=key,
            supported=supported,
            entity_available=entity_available,
            raw=attributes.get(ATTR_TARGET_HUMIDITY_STEP),
            positive=True,
        )

    modes_supported = _humidifier_feature_supported(
        state,
        HumidifierEntityFeature.MODES,
    )
    if key == "mode" and auxiliary.kind == "text":
        return _text_value(
            key=key,
            supported=modes_supported,
            entity_available=entity_available,
            raw=attributes.get("mode"),
        )

    modes = _dehumidifier_modes(
        state,
        supported=modes_supported,
        entity_available=entity_available,
    )
    if key == "mode_count" and auxiliary.kind == "number":
        return AuxiliaryRuntimeValue(
            key=key,
            kind="number",
            supported=modes_supported,
            available=modes is not None,
            number=float(len(modes)) if modes is not None else 0.0,
        )

    if (
        key.startswith("mode_")
        and auxiliary.kind == "text"
        and key[5:].isdigit()
    ):
        slot = int(key[5:])
        available = modes is not None and 1 <= slot <= len(modes)
        return AuxiliaryRuntimeValue(
            key=key,
            kind="text",
            supported=modes_supported,
            available=available,
            text=modes[slot - 1] if available and modes is not None else "",
        )

    if key == "action" and auxiliary.kind == "text":
        supported = state is not None and ATTR_ACTION in attributes
        return _text_value(
            key=key,
            supported=supported,
            entity_available=entity_available,
            raw=attributes.get(ATTR_ACTION),
        )

    return AuxiliaryRuntimeValue(
        key=auxiliary.key,
        kind=auxiliary.kind,
        supported=False,
        available=False,
    )


def resolve_auxiliary_runtime_values(
    control: ControlManifest,
    entity_id: str,
    state: State | None,
    *,
    template: str | None = None,
    template_version: str | None = None,
    temperature_unit: str | None = None,
) -> tuple[AuxiliaryRuntimeValue, ...]:
    """Resolve declared auxiliary values without changing primary state semantics."""
    values: list[AuxiliaryRuntimeValue] = []
    for auxiliary in control.auxiliary_values:
        if (
            template == "climate"
            and template_version in ("1.0", "2.0")
            and entity_id.startswith("climate.")
        ):
            values.append(
                _climate_value(
                    auxiliary,
                    state,
                    temperature_unit=temperature_unit,
                )
            )
            continue
        if (
            template == "dehumidifier"
            and template_version == "2.0"
            and entity_id.startswith("humidifier.")
        ):
            values.append(_dehumidifier_value(auxiliary, state))
            continue
        values.append(_resolve_auxiliary_value(auxiliary, entity_id, state))
    return tuple(values)


def _resolve_auxiliary_value(
    auxiliary: AuxiliaryValueManifest,
    entity_id: str,
    state: State | None,
) -> AuxiliaryRuntimeValue:
    if auxiliary.key == "brightness" and auxiliary.kind == "number":
        return _brightness_value(entity_id, state)

    return AuxiliaryRuntimeValue(
        key=auxiliary.key,
        kind=auxiliary.kind,
        supported=False,
        available=False,
    )


def auxiliary_value_supported(
    control: ControlManifest,
    entity_id: str,
    state: State | None,
    value_key: str,
) -> bool:
    """Return runtime support for one declared auxiliary value."""
    return any(
        value.key == value_key and value.supported
        for value in resolve_auxiliary_runtime_values(control, entity_id, state)
    )
