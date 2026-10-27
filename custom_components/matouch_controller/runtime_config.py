"""Template-aware runtime page configuration helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .const import (
    CONF_CONTROL_SLOT,
    CONF_LABEL,
    CONF_MAPPINGS,
    CONF_PAGE_SLOT,
    CONF_VALIDATED_CONFIG_HASH,
)
from .manifest_model import (
    ControlManifest,
    ManifestError,
    PageManifest,
    RuntimeManifest,
    validate_climate_v1_page,
    validate_climate_v2_page,
    validate_dehumidifier_v2_page,
    validate_lights_v1_page,
    validate_scene_launcher_v1_page,
)
from .mapping_core import (
    MappingError,
    revalidate_generic_mappings,
    validate_generic_mappings,
)


class RuntimePageConfigurationHandler(Protocol):
    """Contract implemented by one mature template runtime configurator."""

    template: str
    template_version: str
    runtime_label_required: bool
    allow_mapping_removal: bool
    allow_partial_configuration: bool

    def controls(self, page: PageManifest) -> tuple[ControlManifest, ...]:
        """Return the controls configured by this template."""

    def is_configured(
        self,
        page: PageManifest,
        mappings: list[dict[str, object]],
    ) -> bool:
        """Return whether one page satisfies this template runtime contract."""

    def validate_persistent_state(
        self,
        page: PageManifest,
        mappings: list[dict[str, Any]],
    ) -> None:
        """Validate template-specific persistent mapping state for one page."""

    def revalidate_persistent_state(
        self,
        page: PageManifest,
        mappings: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Normalize template-specific state after compiled config change."""

    def replace_page_configuration(
        self,
        manifest: RuntimeManifest,
        page: PageManifest,
        existing_mappings: list[dict[str, object]],
        page_mappings: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """Atomically replace one page runtime configuration."""


def _page_addresses(page: PageManifest) -> set[tuple[int, int]]:
    return {
        (control.page_slot, control.control_slot)
        for control in page.controls
    }


def _mapped_page_addresses(
    page: PageManifest,
    mappings: list[dict[str, object]],
) -> set[tuple[int, int]]:
    return {
        (
            int(mapping[CONF_PAGE_SLOT]),
            int(mapping[CONF_CONTROL_SLOT]),
        )
        for mapping in mappings
        if mapping.get(CONF_PAGE_SLOT) == page.page_slot
        and isinstance(mapping.get(CONF_PAGE_SLOT), int)
        and isinstance(mapping.get(CONF_CONTROL_SLOT), int)
    }


def _page_is_fully_mapped(
    page: PageManifest,
    mappings: list[dict[str, object]],
) -> bool:
    return _mapped_page_addresses(page, mappings) == _page_addresses(page)


def _validate_atomic_page_state(
    page: PageManifest,
    mappings: list[dict[str, Any]],
    *,
    contract_name: str,
) -> None:
    mapped = _mapped_page_addresses(page, mappings)
    expected = _page_addresses(page)
    if mapped and mapped != expected:
        count = len(expected)
        raise MappingError(
            f"{contract_name} persistent mapping must be either "
            f"0/{count} or {count}/{count}"
        )


def _revalidate_atomic_page_state(
    page: PageManifest,
    mappings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    mapped = _mapped_page_addresses(page, mappings)
    expected = _page_addresses(page)
    if not mapped or mapped == expected:
        return mappings
    return [
        mapping
        for mapping in mappings
        if mapping.get(CONF_PAGE_SLOT) != page.page_slot
    ]


def _replace_atomic_page_configuration(
    manifest: RuntimeManifest,
    page: PageManifest,
    existing_mappings: list[dict[str, object]],
    page_mappings: list[dict[str, object]],
    *,
    contract_name: str,
    allow_empty: bool = False,
) -> list[dict[str, object]]:
    expected = _page_addresses(page)
    candidate = {
        (
            mapping.get(CONF_PAGE_SLOT),
            mapping.get(CONF_CONTROL_SLOT),
        )
        for mapping in page_mappings
        if isinstance(mapping, Mapping)
    }
    if not page_mappings and allow_empty:
        candidate = set()
    elif candidate != expected:
        raise MappingError(
            f"{contract_name} page candidate must contain every template control"
        )

    combined = [
        dict(mapping)
        for mapping in existing_mappings
        if mapping.get(CONF_PAGE_SLOT) != page.page_slot
    ]
    combined.extend(dict(mapping) for mapping in page_mappings)
    validated = validate_persistent_mappings(manifest, combined)
    return sorted(
        validated,
        key=lambda mapping: (
            int(mapping[CONF_PAGE_SLOT]),
            int(mapping[CONF_CONTROL_SLOT]),
        ),
    )


@dataclass(frozen=True)
class LightsRuntimeConfigurationHandler:
    """Lights 1.0 atomic / 1.1 optional policy over the same two controls."""

    template: str = "lights"
    template_version: str = "1.0"
    runtime_label_required: bool = True
    allow_mapping_removal: bool = False
    allow_partial_configuration: bool = False

    def controls(self, page: PageManifest) -> tuple[ControlManifest, ...]:
        validate_lights_v1_page(page)
        return page.controls

    def is_configured(
        self,
        page: PageManifest,
        mappings: list[dict[str, object]],
    ) -> bool:
        """Apply the explicit version's page configured criterion."""
        validate_lights_v1_page(page)
        if self.allow_partial_configuration:
            return bool(_mapped_page_addresses(page, mappings))
        return _page_is_fully_mapped(page, mappings)

    def validate_persistent_state(
        self,
        page: PageManifest,
        mappings: list[dict[str, Any]],
    ) -> None:
        validate_lights_v1_page(page)
        # The common boundary validates addresses, domains, labels and duplicates.
        if not self.allow_partial_configuration:
            _validate_atomic_page_state(page, mappings, contract_name="Lights")

    def revalidate_persistent_state(
        self,
        page: PageManifest,
        mappings: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        validate_lights_v1_page(page)
        if self.allow_partial_configuration:
            return mappings
        return _revalidate_atomic_page_state(page, mappings)

    def replace_page_configuration(
        self,
        manifest: RuntimeManifest,
        page: PageManifest,
        existing_mappings: list[dict[str, object]],
        page_mappings: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """Atomically publish a valid zero, single or two-control candidate."""
        validate_lights_v1_page(page)
        replace = (
            _replace_optional_page_configuration
            if self.allow_partial_configuration
            else _replace_atomic_page_configuration
        )
        return replace(
            manifest,
            page,
            existing_mappings,
            page_mappings,
            contract_name="Lights",
        )


@dataclass(frozen=True)
class ClimateRuntimeConfigurationHandler:
    """Atomic runtime configuration handler for Climate 1.0/2.0."""

    template: str = "climate"
    template_version: str = "1.0"
    runtime_label_required: bool = False
    allow_mapping_removal: bool = True
    allow_partial_configuration: bool = False

    def _validate_page(self, page: PageManifest) -> None:
        if self.template_version == "2.0":
            validate_climate_v2_page(page)
        elif self.template_version == "1.0":
            validate_climate_v1_page(page)
        else:
            raise ManifestError("Unsupported Climate runtime version")

    def controls(self, page: PageManifest) -> tuple[ControlManifest, ...]:
        self._validate_page(page)
        return page.controls

    def is_configured(
        self,
        page: PageManifest,
        mappings: list[dict[str, object]],
    ) -> bool:
        """Return whether the single Climate control is mapped."""
        self._validate_page(page)
        return _page_is_fully_mapped(page, mappings)

    def _validate_mapping_shape(
        self,
        page: PageManifest,
        mappings: list[dict[str, Any]],
    ) -> None:
        for mapping in mappings:
            if (
                mapping.get(CONF_PAGE_SLOT) == page.page_slot
                and CONF_LABEL in mapping
            ):
                raise MappingError(
                    "Climate persistent mapping must not contain a runtime label"
                )

    def validate_persistent_state(
        self,
        page: PageManifest,
        mappings: list[dict[str, Any]],
    ) -> None:
        self._validate_page(page)
        self._validate_mapping_shape(page, mappings)
        _validate_atomic_page_state(
            page,
            mappings,
            contract_name="Climate",
        )

    def revalidate_persistent_state(
        self,
        page: PageManifest,
        mappings: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        self._validate_page(page)
        self._validate_mapping_shape(page, mappings)
        return _revalidate_atomic_page_state(page, mappings)

    def replace_page_configuration(
        self,
        manifest: RuntimeManifest,
        page: PageManifest,
        existing_mappings: list[dict[str, object]],
        page_mappings: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """Replace or explicitly remove the single Climate mapping atomically."""
        self._validate_page(page)
        return _replace_atomic_page_configuration(
            manifest,
            page,
            existing_mappings,
            page_mappings,
            contract_name="Climate",
            allow_empty=True,
        )


@dataclass(frozen=True)
class DehumidifierRuntimeConfigurationHandler:
    """Atomic runtime configuration handler for Dehumidifier 2.0."""

    template: str = "dehumidifier"
    template_version: str = "2.0"
    runtime_label_required: bool = False
    allow_mapping_removal: bool = True
    allow_partial_configuration: bool = False

    def controls(self, page: PageManifest) -> tuple[ControlManifest, ...]:
        validate_dehumidifier_v2_page(page)
        return page.controls

    def is_configured(
        self,
        page: PageManifest,
        mappings: list[dict[str, object]],
    ) -> bool:
        """Return whether the single Dehumidifier control is mapped."""
        validate_dehumidifier_v2_page(page)
        return _page_is_fully_mapped(page, mappings)

    def _validate_mapping_shape(
        self,
        page: PageManifest,
        mappings: list[dict[str, Any]],
    ) -> None:
        for mapping in mappings:
            if (
                mapping.get(CONF_PAGE_SLOT) == page.page_slot
                and CONF_LABEL in mapping
            ):
                raise MappingError(
                    "Dehumidifier persistent mapping must not contain a runtime label"
                )

    def validate_persistent_state(
        self,
        page: PageManifest,
        mappings: list[dict[str, Any]],
    ) -> None:
        validate_dehumidifier_v2_page(page)
        self._validate_mapping_shape(page, mappings)
        _validate_atomic_page_state(
            page,
            mappings,
            contract_name="Dehumidifier",
        )

    def revalidate_persistent_state(
        self,
        page: PageManifest,
        mappings: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        validate_dehumidifier_v2_page(page)
        self._validate_mapping_shape(page, mappings)
        return _revalidate_atomic_page_state(page, mappings)

    def replace_page_configuration(
        self,
        manifest: RuntimeManifest,
        page: PageManifest,
        existing_mappings: list[dict[str, object]],
        page_mappings: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """Replace or remove the single Dehumidifier mapping atomically."""
        validate_dehumidifier_v2_page(page)
        return _replace_atomic_page_configuration(
            manifest,
            page,
            existing_mappings,
            page_mappings,
            contract_name="Dehumidifier",
            allow_empty=True,
        )


@dataclass(frozen=True)
class SceneLauncherRuntimeConfigurationHandler:
    """Independent optional mappings for the four Scene Launcher slots."""

    template: str = "scene_launcher"
    template_version: str = "1.0"
    runtime_label_required: bool = True
    allow_mapping_removal: bool = True
    allow_partial_configuration: bool = True

    def controls(self, page: PageManifest) -> tuple[ControlManifest, ...]:
        validate_scene_launcher_v1_page(page)
        return page.controls

    def is_configured(
        self, page: PageManifest, mappings: list[dict[str, object]]
    ) -> bool:
        validate_scene_launcher_v1_page(page)
        return bool(_mapped_page_addresses(page, mappings))

    def validate_persistent_state(
        self, page: PageManifest, mappings: list[dict[str, Any]]
    ) -> None:
        validate_scene_launcher_v1_page(page)
        # Address, domain, labels and duplicates are validated by the common
        # mapping boundary. No full-page coverage is required for this template.

    def revalidate_persistent_state(
        self, page: PageManifest, mappings: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        validate_scene_launcher_v1_page(page)
        return mappings

    def replace_page_configuration(
        self,
        manifest: RuntimeManifest,
        page: PageManifest,
        existing_mappings: list[dict[str, object]],
        page_mappings: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        validate_scene_launcher_v1_page(page)
        return _replace_optional_page_configuration(
            manifest, page, existing_mappings, page_mappings,
            contract_name="Scene Launcher",
        )


def _replace_optional_page_configuration(
    manifest: RuntimeManifest,
    page: PageManifest,
    existing_mappings: list[dict[str, object]],
    page_mappings: list[dict[str, object]],
    *,
    contract_name: str,
) -> list[dict[str, object]]:
    """Shared optional-slot publication, preserving unrelated order and no-ops."""
    if any(
        mapping.get(CONF_PAGE_SLOT) != page.page_slot
        for mapping in page_mappings
    ):
        raise MappingError(f"{contract_name} candidate belongs to another page")
    combined = [
        dict(mapping) for mapping in existing_mappings
        if mapping.get(CONF_PAGE_SLOT) != page.page_slot
    ]
    combined.extend(dict(mapping) for mapping in page_mappings)
    validated = validate_persistent_mappings(manifest, combined)
    existing_order = {
        (mapping[CONF_PAGE_SLOT], mapping[CONF_CONTROL_SLOT]): index
        for index, mapping in enumerate(existing_mappings)
    }
    return sorted(
        validated,
        key=lambda mapping: existing_order.get(
            (mapping[CONF_PAGE_SLOT], mapping[CONF_CONTROL_SLOT]),
            len(existing_order) + int(mapping[CONF_CONTROL_SLOT]),
        ),
    )


_RUNTIME_CONFIGURATION_HANDLERS: dict[
    tuple[str, str], RuntimePageConfigurationHandler
] = {
    ("lights", "1.0"): LightsRuntimeConfigurationHandler(),
    ("lights", "1.1"): LightsRuntimeConfigurationHandler(
        template_version="1.1", allow_mapping_removal=True,
        allow_partial_configuration=True,
    ),
    ("climate", "1.0"): ClimateRuntimeConfigurationHandler(),
    ("climate", "2.0"): ClimateRuntimeConfigurationHandler(template_version="2.0"),
    ("dehumidifier", "2.0"): DehumidifierRuntimeConfigurationHandler(),
    ("scene_launcher", "1.0"): SceneLauncherRuntimeConfigurationHandler(),
}


def runtime_configuration_handler(
    page: PageManifest,
) -> RuntimePageConfigurationHandler:
    """Return the mature runtime handler for one explicit template/version."""
    handler = _RUNTIME_CONFIGURATION_HANDLERS.get(
        (page.template, page.template_version)
    )
    if handler is None:
        raise ManifestError(
            "Unsupported runtime configuration template/version: "
            f"{page.template}/{page.template_version}"
        )
    return handler


def validate_persistent_mappings(
    manifest: RuntimeManifest,
    mappings: Any,
) -> list[dict[str, Any]]:
    """Validate generic mappings, then delegate template policy per page."""
    normalized = validate_generic_mappings(manifest, mappings)
    for page in manifest.pages:
        handler = runtime_configuration_handler(page)
        if handler.runtime_label_required:
            for mapping in normalized:
                if (
                    mapping.get(CONF_PAGE_SLOT) == page.page_slot
                    and CONF_LABEL not in mapping
                ):
                    raise MappingError(
                        f"{page.template} persistent mapping requires a runtime label"
                    )
        handler.validate_persistent_state(page, normalized)
    return normalized


def revalidate_persistent_mappings(
    manifest: RuntimeManifest,
    mappings: Any,
) -> list[dict[str, Any]]:
    """Revalidate generically, then delegate template-specific recovery."""
    normalized = revalidate_generic_mappings(manifest, mappings)
    for page in manifest.pages:
        normalized = runtime_configuration_handler(
            page
        ).revalidate_persistent_state(page, normalized)
    return validate_persistent_mappings(manifest, normalized)


def page_mappings(
    mappings: list[dict[str, object]],
    page: PageManifest,
) -> list[dict[str, object]]:
    """Return mappings belonging to one page in template control order."""
    by_slot = {
        mapping.get(CONF_CONTROL_SLOT): mapping
        for mapping in mappings
        if mapping.get(CONF_PAGE_SLOT) == page.page_slot
    }
    return [
        dict(by_slot[control.control_slot])
        for control in page.controls
        if control.control_slot in by_slot
    ]


def page_is_configured(
    mappings: list[dict[str, object]],
    page: PageManifest,
) -> bool:
    """Delegate configured-state semantics to the page template handler."""
    return runtime_configuration_handler(page).is_configured(page, mappings)


def validate_or_revalidate_entry_mappings(
    manifest: RuntimeManifest,
    effective: Mapping[str, object],
) -> list[dict[str, object]]:
    """Validate an entry through the template-aware runtime boundary."""
    raw_mappings = effective.get(CONF_MAPPINGS, [])
    if effective.get(CONF_VALIDATED_CONFIG_HASH) == manifest.config_hash:
        return validate_persistent_mappings(manifest, raw_mappings)
    return revalidate_persistent_mappings(manifest, raw_mappings)
