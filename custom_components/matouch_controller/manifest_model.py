"""Runtime manifest model for MaTouch Controller."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
from typing import Any
import unicodedata
from uuid import UUID

from .const import PROTOCOL_VERSION, SCHEMA_VERSION


VALID_FUNCTIONS = frozenset({
    "toggle",
    "turn_on",
    "turn_off",
    "press",
    "set_value",
    "increment",
    "decrement",
    "select_option",
    "open",
    "close",
    "stop",
})

VALID_STATE_KINDS = frozenset({
    "none",
    "binary",
    "number",
    "text",
    "option",
})

PRODUCT_TEMPLATES = frozenset({
    "lights",
    "climate",
    "dehumidifier",
    "sensor_dashboard",
    "value_control",
    "scene_launcher",
})

# Core §4.3: a manifest declaring any other template is invalid. The retired
# button_grid MVP is historical provenance only (D047 pct. 3).
VALID_TEMPLATES = PRODUCT_TEMPLATES

_PRODUCT_PROFILE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{2,63}$")
_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+$")
_TEMPLATE_LIBRARY_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_DOMAIN_PATTERN = re.compile(r"^[a-z][a-z0-9_]+$")
_ICON_PATTERN = re.compile(r"^mdi:[a-z0-9-]+$")
_AUXILIARY_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_CONFIG_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")

FUNCTIONAL_PAGE_HEADER_MAX_LENGTH = 16
_FUNCTIONAL_PAGE_HEADER_EXTRA_CHARS = frozenset("ĂăÂâÎîȘșȚțŞşŢţ")


class ManifestError(ValueError):
    """Raised when a MaTouch runtime manifest is invalid."""


def normalize_functional_page_header(
    value: Any,
    context: str = "functional page header",
) -> str:
    """Normalize and validate Functional Page Header Latin v1."""
    if not isinstance(value, str):
        raise ManifestError(f"Invalid {context}")

    normalized = unicodedata.normalize("NFC", value)
    if not normalized.strip() or len(normalized) > FUNCTIONAL_PAGE_HEADER_MAX_LENGTH:
        raise ManifestError(f"Invalid {context}")

    if any(
        not (0x20 <= ord(character) <= 0x7E)
        and character not in _FUNCTIONAL_PAGE_HEADER_EXTRA_CHARS
        for character in normalized
    ):
        raise ManifestError(f"Invalid {context}")

    return normalized


@dataclass(frozen=True)
class AuxiliaryValueManifest:
    """One named typed auxiliary value exposed by a generic control."""

    key: str
    kind: str


@dataclass(frozen=True)
class ControlManifest:
    """Generic control exposed by the MaTouch firmware."""

    page_slot: int
    control_slot: int
    function: str
    state_kind: str
    icon: str
    allowed_domains: tuple[str, ...]
    optimistic: bool
    confirmation_timeout_ms: int
    auxiliary_values: tuple[AuxiliaryValueManifest, ...] = ()


@dataclass(frozen=True)
class PageManifest:
    """Generic page exposed by the MaTouch firmware."""

    page_slot: int
    template: str
    template_version: str
    title: str
    controls: tuple[ControlManifest, ...]


@dataclass(frozen=True)
class RuntimeManifest:
    """Validated subset of the MaTouch runtime manifest."""

    device_uid: str
    product_profile: str
    protocol_version: str
    schema_version: str
    firmware_version: str
    template_library_version: str
    config_hash: str
    pages: tuple[PageManifest, ...]

    @property
    def controls(self) -> tuple[ControlManifest, ...]:
        """Return all controls while preserving page order."""
        return tuple(control for page in self.pages for control in page.controls)

    def control_at(
        self,
        page_slot: int,
        control_slot: int,
    ) -> ControlManifest | None:
        """Return the control at the canonical slot address."""
        for control in self.controls:
            if (
                control.page_slot == page_slot
                and control.control_slot == control_slot
            ):
                return control
        return None


def _require_fields(
    data: dict[str, Any], fields: tuple[str, ...], context: str
) -> None:
    missing = [field for field in fields if field not in data]
    if missing:
        raise ManifestError(f"Missing {context} fields: {', '.join(missing)}")


def _page_slot(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 6:
        raise ManifestError("Invalid page slot: expected integer 1..6")
    return value


def _control_slot(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ManifestError("Invalid control slot: expected positive integer")
    return value


def _bounded_string(
    value: Any,
    context: str,
    *,
    minimum: int = 1,
    maximum: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if (
        not isinstance(value, str)
        or not minimum <= len(value) <= maximum
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        raise ManifestError(f"Invalid {context}")
    return value


def _device_uid(value: Any) -> str:
    if not isinstance(value, str):
        raise ManifestError("Invalid device_uid")

    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as err:
        raise ManifestError("Invalid device_uid") from err

    if str(parsed) != value:
        raise ManifestError("Invalid device_uid")

    return value


def _protocol_version(value: Any) -> str:
    version = _bounded_string(
        value,
        "protocol_version",
        maximum=32,
        pattern=_VERSION_PATTERN,
    )
    current_major = PROTOCOL_VERSION.split(".", 1)[0]
    incoming_major = version.split(".", 1)[0]
    if incoming_major != current_major:
        raise ManifestError(f"Unsupported protocol_version: {version}")
    return version


def validate_lights_v1_page(page: PageManifest) -> None:
    """Validate the shared compiled shape of explicit Lights 1.0 and 1.1."""
    if page.template != "lights" or page.template_version not in {"1.0", "1.1"}:
        raise ManifestError("Lights v1 requires template lights/1.0 or lights/1.1")

    if len(page.controls) != 2:
        raise ManifestError("Lights v1 requires exactly 2 controls")

    if {control.control_slot for control in page.controls} != {1, 2}:
        raise ManifestError("Lights v1 requires control slots 1 and 2")

    expected_auxiliary = (AuxiliaryValueManifest("brightness", "number"),)
    for control in page.controls:
        context = (
            f"page_slot={control.page_slot},control_slot={control.control_slot}"
        )
        if control.function != "toggle":
            raise ManifestError(f"Lights v1 requires toggle function for {context}")
        if control.state_kind != "binary":
            raise ManifestError(f"Lights v1 requires binary state for {context}")
        if set(control.allowed_domains) != {"switch", "light"} or len(
            control.allowed_domains
        ) != 2:
            raise ManifestError(
                f"Lights v1 requires switch and light domains for {context}"
            )
        if not control.optimistic:
            raise ManifestError(f"Lights v1 requires optimistic mode for {context}")
        if control.confirmation_timeout_ms != 2000:
            raise ManifestError(f"Lights v1 requires 2000 ms timeout for {context}")
        if control.auxiliary_values != expected_auxiliary:
            raise ManifestError(
                f"Lights v1 requires brightness:number auxiliary value for {context}"
            )


def _validate_climate_page(page: PageManifest, version: str) -> None:
    """Validate common topology plus the exact versioned auxiliary shape."""
    if page.template != "climate" or page.template_version != version:
        raise ManifestError(f"Climate requires template climate/{version}")

    if len(page.controls) != 1:
        raise ManifestError("Climate requires exactly 1 control")

    control = page.controls[0]
    context = f"page_slot={control.page_slot},control_slot={control.control_slot}"
    if control.control_slot != 1:
        raise ManifestError("Climate requires control_slot 1")
    if control.function != "select_option":
        raise ManifestError(
            f"Climate requires select_option function for {context}"
        )
    if control.state_kind != "option":
        raise ManifestError(f"Climate requires option state for {context}")
    if control.allowed_domains != ("climate",):
        raise ManifestError(f"Climate requires climate domain for {context}")
    if not control.optimistic:
        raise ManifestError(f"Climate requires optimistic mode for {context}")
    if control.confirmation_timeout_ms != 2000:
        raise ManifestError(f"Climate requires 2000 ms timeout for {context}")

    expected_auxiliary = (
        AuxiliaryValueManifest("hvac_modes_mask", "number"),
        AuxiliaryValueManifest("target_temperature", "number"),
        AuxiliaryValueManifest("current_temperature", "number"),
        AuxiliaryValueManifest("min_temp", "number"),
        AuxiliaryValueManifest("max_temp", "number"),
        AuxiliaryValueManifest("target_temp_step", "number"),
        AuxiliaryValueManifest("temperature_unit", "text"),
        AuxiliaryValueManifest("fan_mode", "text"),
        AuxiliaryValueManifest("fan_mode_index", "number"),
        AuxiliaryValueManifest("fan_mode_count", "number"),
        AuxiliaryValueManifest("hvac_action", "text"),
        AuxiliaryValueManifest("current_humidity", "number"),
        AuxiliaryValueManifest("target_temp_low", "number"),
        AuxiliaryValueManifest("target_temp_high", "number"),
    )
    if version == "2.0":
        expected_auxiliary += (AuxiliaryValueManifest("power_on", "number"),)
    if control.auxiliary_values != expected_auxiliary:
        raise ManifestError(
            f"Climate requires the exact auxiliary contract for {context}"
        )


def validate_climate_v1_page(page: PageManifest) -> None:
    """Validate the explicit legacy Climate 1.0 contract."""
    _validate_climate_page(page, "1.0")


def validate_climate_v2_page(page: PageManifest) -> None:
    """Validate Climate 2.0 with its appended TURN_ON capability."""
    _validate_climate_page(page, "2.0")


def validate_dehumidifier_v2_page(page: PageManifest) -> None:
    """Validate the mature Dehumidifier 2.0 single-control contract."""
    if page.template != "dehumidifier" or page.template_version != "2.0":
        raise ManifestError(
            "Dehumidifier v2 requires template dehumidifier/2.0"
        )

    if len(page.controls) != 1:
        raise ManifestError("Dehumidifier v2 requires exactly 1 control")

    control = page.controls[0]
    context = f"page_slot={control.page_slot},control_slot={control.control_slot}"
    if control.control_slot != 1:
        raise ManifestError("Dehumidifier v2 requires control_slot 1")
    if control.function != "toggle":
        raise ManifestError(
            f"Dehumidifier v2 requires toggle function for {context}"
        )
    if control.state_kind != "binary":
        raise ManifestError(
            f"Dehumidifier v2 requires binary state for {context}"
        )
    if control.allowed_domains != ("humidifier",):
        raise ManifestError(
            f"Dehumidifier v2 requires humidifier domain for {context}"
        )
    if not control.optimistic:
        raise ManifestError(
            f"Dehumidifier v2 requires optimistic mode for {context}"
        )
    if control.confirmation_timeout_ms != 2000:
        raise ManifestError(
            f"Dehumidifier v2 requires 2000 ms timeout for {context}"
        )

    expected_auxiliary = (
        AuxiliaryValueManifest("target_humidity", "number"),
        AuxiliaryValueManifest("current_humidity", "number"),
        AuxiliaryValueManifest("min_humidity", "number"),
        AuxiliaryValueManifest("max_humidity", "number"),
        AuxiliaryValueManifest("target_humidity_step", "number"),
        AuxiliaryValueManifest("mode", "text"),
        AuxiliaryValueManifest("mode_count", "number"),
        AuxiliaryValueManifest("mode_1", "text"),
        AuxiliaryValueManifest("mode_2", "text"),
        AuxiliaryValueManifest("mode_3", "text"),
        AuxiliaryValueManifest("mode_4", "text"),
        AuxiliaryValueManifest("mode_5", "text"),
        AuxiliaryValueManifest("mode_6", "text"),
        AuxiliaryValueManifest("action", "text"),
    )
    if control.auxiliary_values != expected_auxiliary:
        raise ManifestError(
            f"Dehumidifier v2 requires the exact auxiliary contract for {context}"
        )


def validate_scene_launcher_v1_page(page: PageManifest) -> None:
    """Validate Scene Launcher 1.0's four independent action slots."""
    if page.template != "scene_launcher" or page.template_version != "1.0":
        raise ManifestError("Scene Launcher v1 requires scene_launcher/1.0")
    if len(page.controls) != 4 or {
        control.control_slot for control in page.controls
    } != {1, 2, 3, 4}:
        raise ManifestError("Scene Launcher v1 requires exactly slots 1..4")
    for control in page.controls:
        if (
            control.function != "press"
            or control.state_kind != "none"
            or control.icon != "mdi:play"
            or set(control.allowed_domains) != {"scene", "script"}
            or len(control.allowed_domains) != 2
            or control.optimistic
            or control.confirmation_timeout_ms != 2000
            or control.auxiliary_values
        ):
            raise ManifestError(
                "Scene Launcher v1 requires press/none, mdi:play, scene/script, "
                "optimistic=false, 2000 ms and no auxiliary values"
            )


_RUNTIME_TEMPLATE_VALIDATORS: dict[
    tuple[str, str], Callable[[PageManifest], None]
] = {
    ("lights", "1.0"): validate_lights_v1_page,
    ("lights", "1.1"): validate_lights_v1_page,
    ("climate", "1.0"): validate_climate_v1_page,
    ("climate", "2.0"): validate_climate_v2_page,
    ("dehumidifier", "2.0"): validate_dehumidifier_v2_page,
    ("scene_launcher", "1.0"): validate_scene_launcher_v1_page,
}


def runtime_template_supported(template: str, template_version: str) -> bool:
    """Return whether a structurally valid template/version has runtime support."""
    return (template, template_version) in _RUNTIME_TEMPLATE_VALIDATORS


def validate_runtime_manifest_templates(manifest: RuntimeManifest) -> None:
    """Validate every page against an explicitly supported runtime contract."""
    for page in manifest.pages:
        validator = _RUNTIME_TEMPLATE_VALIDATORS.get(
            (page.template, page.template_version)
        )
        if validator is None:
            raise ManifestError(
                "Unsupported runtime template/version: "
                f"{page.template}/{page.template_version}"
            )
        validator(page)


def parse_runtime_manifest(data: dict[str, Any]) -> RuntimeManifest:
    """Parse and validate the structural runtime manifest contract."""
    if not isinstance(data, dict):
        raise ManifestError("Manifest must be an object")

    required = (
        "device_uid",
        "product_profile",
        "protocol_version",
        "schema_version",
        "firmware_version",
        "template_library_version",
        "config_hash",
        "pages",
    )
    _require_fields(data, required, "manifest")

    protocol_version = _protocol_version(data["protocol_version"])

    if data["schema_version"] != SCHEMA_VERSION:
        raise ManifestError(
            f"Unsupported schema_version: {data['schema_version']}"
        )

    device_uid = _device_uid(data["device_uid"])
    product_profile = _bounded_string(
        data["product_profile"],
        "product_profile",
        maximum=64,
        pattern=_PRODUCT_PROFILE_PATTERN,
    )
    firmware_version = _bounded_string(
        data["firmware_version"],
        "firmware_version",
        maximum=32,
    )
    template_library_version = _bounded_string(
        data["template_library_version"],
        "template_library_version",
        maximum=32,
        pattern=_TEMPLATE_LIBRARY_VERSION_PATTERN,
    )
    config_hash = _bounded_string(
        data["config_hash"],
        "config_hash",
        minimum=64,
        maximum=64,
        pattern=_CONFIG_HASH_PATTERN,
    )

    raw_pages = data["pages"]
    if not isinstance(raw_pages, list) or not 1 <= len(raw_pages) <= 6:
        raise ManifestError("pages must be a list with 1..6 items")

    pages: list[PageManifest] = []
    seen_page_slots: set[int] = set()

    for raw_page in raw_pages:
        if not isinstance(raw_page, dict):
            raise ManifestError("Each page must be an object")

        _require_fields(
            raw_page,
            ("slot", "template", "template_version", "title", "controls"),
            "page",
        )

        page_slot = _page_slot(raw_page["slot"])
        if page_slot in seen_page_slots:
            raise ManifestError(f"Duplicate page slot: {page_slot}")

        template = raw_page["template"]
        if not isinstance(template, str) or template not in VALID_TEMPLATES:
            raise ManifestError(f"Invalid template for page slot {page_slot}")

        template_version = _bounded_string(
            raw_page["template_version"],
            f"template_version for page slot {page_slot}",
            maximum=32,
            pattern=_VERSION_PATTERN,
        )
        title = normalize_functional_page_header(
            raw_page["title"],
            f"title for page slot {page_slot}",
        )

        seen_page_slots.add(page_slot)

        raw_controls = raw_page["controls"]
        if not isinstance(raw_controls, list):
            raise ManifestError(
                f"controls for page slot {page_slot} must be a list"
            )

        controls: list[ControlManifest] = []
        seen_control_slots: set[int] = set()

        for raw_control in raw_controls:
            if not isinstance(raw_control, dict):
                raise ManifestError(
                    f"Each control in page slot {page_slot} must be an object"
                )

            _require_fields(
                raw_control,
                (
                    "slot",
                    "function",
                    "state_kind",
                    "icon",
                    "allowed_domains",
                    "optimistic",
                    "confirmation_timeout_ms",
                ),
                "control",
            )

            control_slot = _control_slot(raw_control["slot"])
            if control_slot in seen_control_slots:
                raise ManifestError(
                    f"Duplicate control slot {control_slot} in page slot {page_slot}"
                )

            function = raw_control["function"]
            if not isinstance(function, str) or function not in VALID_FUNCTIONS:
                raise ManifestError(f"Invalid function: {function}")

            state_kind = raw_control["state_kind"]
            if (
                not isinstance(state_kind, str)
                or state_kind not in VALID_STATE_KINDS
            ):
                raise ManifestError(f"Invalid state_kind: {state_kind}")

            context = f"page_slot={page_slot},control_slot={control_slot}"
            icon = _bounded_string(
                raw_control["icon"],
                f"icon for {context}",
                maximum=64,
                pattern=_ICON_PATTERN,
            )

            allowed_domains = raw_control["allowed_domains"]
            if (
                not isinstance(allowed_domains, list)
                or not allowed_domains
                or any(
                    not isinstance(domain, str)
                    or _DOMAIN_PATTERN.fullmatch(domain) is None
                    for domain in allowed_domains
                )
                or len(set(allowed_domains)) != len(allowed_domains)
            ):
                raise ManifestError(f"Invalid allowed_domains for {context}")

            optimistic = raw_control["optimistic"]
            if not isinstance(optimistic, bool):
                raise ManifestError(f"Invalid optimistic for {context}")

            confirmation_timeout_ms = raw_control["confirmation_timeout_ms"]
            if (
                isinstance(confirmation_timeout_ms, bool)
                or not isinstance(confirmation_timeout_ms, int)
                or not 1000 <= confirmation_timeout_ms <= 30000
            ):
                raise ManifestError(
                    f"Invalid confirmation_timeout_ms for {context}"
                )

            raw_auxiliary_values = raw_control.get("auxiliary_values", [])
            if not isinstance(raw_auxiliary_values, list):
                raise ManifestError(f"Invalid auxiliary_values for {context}")

            auxiliary_values: list[AuxiliaryValueManifest] = []
            seen_auxiliary_keys: set[str] = set()
            for raw_auxiliary in raw_auxiliary_values:
                if not isinstance(raw_auxiliary, dict):
                    raise ManifestError(
                        f"Each auxiliary value for {context} must be an object"
                    )
                _require_fields(raw_auxiliary, ("key", "kind"), "auxiliary value")
                key = _bounded_string(
                    raw_auxiliary["key"],
                    f"auxiliary key for {context}",
                    maximum=32,
                    pattern=_AUXILIARY_KEY_PATTERN,
                )
                kind = raw_auxiliary["kind"]
                if not isinstance(kind, str) or kind not in VALID_STATE_KINDS:
                    raise ManifestError(
                        f"Invalid auxiliary kind for {context}: {kind}"
                    )
                if key in seen_auxiliary_keys:
                    raise ManifestError(
                        f"Duplicate auxiliary key {key} for {context}"
                    )
                seen_auxiliary_keys.add(key)
                auxiliary_values.append(AuxiliaryValueManifest(key, kind))

            seen_control_slots.add(control_slot)
            controls.append(
                ControlManifest(
                    page_slot=page_slot,
                    control_slot=control_slot,
                    function=function,
                    state_kind=state_kind,
                    icon=icon,
                    allowed_domains=tuple(allowed_domains),
                    optimistic=optimistic,
                    confirmation_timeout_ms=confirmation_timeout_ms,
                    auxiliary_values=tuple(auxiliary_values),
                )
            )

        pages.append(
            PageManifest(
                page_slot=page_slot,
                template=template,
                template_version=template_version,
                title=title,
                controls=tuple(controls),
            )
        )

    return RuntimeManifest(
        device_uid=device_uid,
        product_profile=product_profile,
        protocol_version=protocol_version,
        schema_version=data["schema_version"],
        firmware_version=firmware_version,
        template_library_version=template_library_version,
        config_hash=config_hash,
        pages=tuple(pages),
    )

