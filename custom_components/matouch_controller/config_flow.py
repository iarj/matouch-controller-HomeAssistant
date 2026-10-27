"""Config and options flows for MaTouch Controller."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ENTITY_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .const import (
    CONF_CONFIG_REVISION,
    CONF_CONTROL_SLOT,
    CONF_DEVICE_UID,
    CONF_DISPLAY_NAME,
    CONF_LABEL,
    CONF_MANIFEST_ACTION,
    CONF_MAPPINGS,
    CONF_PAGE_SLOT,
    CONF_REMOVE_MAPPING,
    CONF_VALIDATED_CONFIG_HASH,
    DEFAULT_DISPLAY_NAME,
    DISPLAY_NAME_MAX_LENGTH,
    DOMAIN,
)
from .manifest_model import (
    ControlManifest,
    ManifestError,
    PageManifest,
    RuntimeManifest,
    validate_runtime_manifest_templates,
)
from .mapping import (
    MappingError,
    RuntimeLabelError,
    effective_entry_data,
    mapping_for_slots,
    mapping_record,
    persist_manifest_action,
    validate_runtime_label,
)
from .runtime_config import (
    RuntimePageConfigurationHandler,
    page_is_configured,
    page_mappings,
    runtime_configuration_handler,
    validate_or_revalidate_entry_mappings,
)
from .transport import (
    ManifestTransportResponseError,
    ManifestTransportUnavailableError,
    async_fetch_runtime_manifest,
    available_manifest_actions,
)

# Options menu items next to the numeric page slots (HA UX apply pt. 2).
# Values are also translation keys, so they follow the lowercase key format.
MENU_DISPLAY_NAME = "display_name"
MENU_DONE = "done"
SLOT_MENU_BACK = "back"
MENU_TRANSLATION_KEY = "options_menu"
SLOT_MENU_TRANSLATION_KEY = "slot_menu"

# Language-neutral page/slot state marks; the step descriptions explain them.
CONFIGURED_MARK = "✓"
UNCONFIGURED_MARK = "○"


def _persistent_form_base(
    entry: config_entries.ConfigEntry,
    *,
    include_title: bool = False,
) -> tuple[dict[str, Any], str | None]:
    """Detach the persistent values a form may later replace.

    Revision alone misses metadata/manifest revalidation without a revision bump.
    Preserve the effective configuration, including nested mappings, before any
    manifest/identity await. Options also derives its name from the entry title.
    Device-local active/order observations and runtime sessions are not included.
    """
    return (
        deepcopy(effective_entry_data(entry)),
        getattr(entry, "title", None) if include_title else None,
    )


def build_mapping_schema(
    control: ControlManifest,
    *,
    entity_default: str | None = None,
    label_default: str | None = None,
    runtime_label_required: bool = True,
    allow_mapping_removal: bool = False,
    removal_without_fields: bool = False,
) -> vol.Schema:
    """Build the template-owned user-editable runtime fields for one control."""
    fields: dict[vol.Marker, Any] = {}

    entity_marker: vol.Marker
    field_marker = (
        vol.Optional if allow_mapping_removal and removal_without_fields
        else vol.Required
    )
    if entity_default is None:
        entity_marker = field_marker(CONF_ENTITY_ID)
    else:
        entity_marker = field_marker(CONF_ENTITY_ID, default=entity_default)

    fields[entity_marker] = selector.EntitySelector(
        selector.EntitySelectorConfig(
            domain=list(control.allowed_domains),
        )
    )

    if runtime_label_required:
        label_marker: vol.Marker
        if label_default is None:
            label_marker = field_marker(CONF_LABEL)
        else:
            label_marker = field_marker(CONF_LABEL, default=label_default)
        fields[label_marker] = str

    if allow_mapping_removal:
        fields[vol.Optional(CONF_REMOVE_MAPPING, default=False)] = bool

    return vol.Schema(fields)


def build_display_name_schema(display_name_default: str) -> vol.Schema:
    """Build the Home Assistant-local device metadata step."""
    return vol.Schema(
        {
            vol.Required(
                CONF_DISPLAY_NAME,
                default=display_name_default,
            ): str,
        }
    )


def build_transport_schema(actions: tuple[str, ...]) -> vol.Schema:
    """Build the ESPHome manifest action selection schema."""
    return vol.Schema(
        {
            vol.Required(CONF_MANIFEST_ACTION): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(actions),
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        }
    )


def _slot_selection_schema(
    field: str,
    options: list[dict[str, str]],
    *,
    translation_key: str | None = None,
) -> vol.Schema:
    """Build one human-readable page/control selector.

    With a translation key, the frontend translates the static items; dynamic
    page/slot items without a translation keep the label built here.
    """
    config: dict[str, Any] = {
        "options": options,
        "mode": selector.SelectSelectorMode.DROPDOWN,
    }
    if translation_key is not None:
        config["translation_key"] = translation_key
    return vol.Schema(
        {
            vol.Required(field): selector.SelectSelector(
                selector.SelectSelectorConfig(**config)
            )
        }
    )


def _page_option(
    page: PageManifest,
    mappings: list[dict[str, object]],
) -> dict[str, str]:
    """Return one page item with its template-defined configured state."""
    mark = (
        CONFIGURED_MARK
        if page_is_configured(mappings, page)
        else UNCONFIGURED_MARK
    )
    return {
        "value": str(page.page_slot),
        "label": f"{page.page_slot}: {page.title} · {page.template} · {mark}",
    }


def _selected_slot(value: Any, allowed: set[int]) -> int | None:
    """Return one selected slot if it belongs to the current manifest scope."""
    if not isinstance(value, str) or not value.isdigit():
        return None
    slot = int(value)
    return slot if slot in allowed else None


def _validate_display_name(value: Any) -> tuple[str | None, str | None]:
    """Validate the Home Assistant-local display name."""
    if not isinstance(value, str) or not value.strip():
        return None, "display_name_required"

    normalized = value.strip()
    if len(normalized) > DISPLAY_NAME_MAX_LENGTH:
        return None, "display_name_too_long"

    return normalized, None


def validate_mapping_entity(
    hass: HomeAssistant,
    control: ControlManifest,
    entity_id: Any,
    seen_entities_on_page: set[str],
) -> str:
    """Validate one entity selected for a manifest-defined control."""
    if not isinstance(entity_id, str) or "." not in entity_id:
        raise MappingError(
            "Invalid entity_id for "
            f"page_slot={control.page_slot},control_slot={control.control_slot}"
        )

    domain = entity_id.split(".", 1)[0]
    if domain not in control.allowed_domains:
        raise MappingError(
            "Entity domain not allowed for "
            f"page_slot={control.page_slot},control_slot={control.control_slot}: "
            f"{domain}"
        )

    if hass.states.get(entity_id) is None:
        raise MappingError(f"Entity does not exist: {entity_id}")

    if entity_id in seen_entities_on_page:
        raise MappingError(
            f"Duplicate entity mapping in page {control.page_slot}: {entity_id}"
        )

    return entity_id


class _PageConfigurationFlowMixin:
    """Shared page-first/template-aware runtime configuration behavior."""

    _runtime_manifest: RuntimeManifest | None
    _working_mappings: list[dict[str, object]]
    _selected_page: PageManifest | None
    _page_handler: RuntimePageConfigurationHandler | None
    _page_controls: tuple[ControlManifest, ...]
    _page_control_index: int
    _page_candidate: list[dict[str, object]]
    _display_name: str

    def _initialize_page_configuration(
        self,
        manifest: RuntimeManifest,
        mappings: list[dict[str, object]] | None = None,
    ) -> None:
        self._runtime_manifest = manifest
        self._working_mappings = [
            dict(mapping) for mapping in (mappings or [])
        ]
        self._selected_page = None
        self._page_handler = None
        self._page_controls = ()
        self._page_control_index = 0
        self._page_candidate = []

    def _begin_page_configuration(self, page: PageManifest) -> None:
        handler = runtime_configuration_handler(page)
        controls = handler.controls(page)
        if not controls:
            raise ManifestError(
                f"Runtime configuration template has no controls: {page.template}"
            )

        self._selected_page = page
        self._page_handler = handler
        self._page_controls = controls
        self._page_control_index = 0
        self._page_candidate = (
            page_mappings(self._working_mappings, page)
            if handler.allow_partial_configuration else []
        )

    def _current_control(self) -> ControlManifest | None:
        if not self._page_controls:
            return None
        if not 0 <= self._page_control_index < len(self._page_controls):
            return None
        return self._page_controls[self._page_control_index]

    def _current_existing_mapping(self) -> Mapping[str, Any] | None:
        page = self._selected_page
        control = self._current_control()
        if page is None or control is None:
            return None
        return mapping_for_slots(
            self._page_candidate
            if self._page_handler is not None
            and self._page_handler.allow_partial_configuration
            else self._working_mappings,
            page.page_slot,
            control.control_slot,
        )

    def _seen_candidate_entities(self) -> set[str]:
        control = self._current_control()
        return {
            str(mapping[CONF_ENTITY_ID])
            for mapping in self._page_candidate
            if isinstance(mapping.get(CONF_ENTITY_ID), str)
            and (
                self._page_handler is None
                or not self._page_handler.allow_partial_configuration
                or control is None
                or mapping.get(CONF_CONTROL_SLOT)
                != control.control_slot
            )
        }

    def _validate_current_page_mapping(
        self,
        hass: HomeAssistant,
        user_input: dict[str, Any],
    ) -> tuple[dict[str, object] | None, bool, dict[str, str]]:
        control = self._current_control()
        handler = self._page_handler
        if control is None or handler is None:
            return None, False, {"base": "invalid_mapping"}

        if (
            handler.allow_mapping_removal
            and self._current_existing_mapping() is not None
            and user_input.get(CONF_REMOVE_MAPPING) is True
        ):
            return None, True, {}

        errors: dict[str, str] = {}
        try:
            entity_id = validate_mapping_entity(
                hass,
                control,
                user_input.get(CONF_ENTITY_ID),
                self._seen_candidate_entities(),
            )
            label = (
                validate_runtime_label(user_input.get(CONF_LABEL))
                if handler.runtime_label_required
                else None
            )
        except RuntimeLabelError as err:
            errors[CONF_LABEL] = err.translation_key
        except MappingError:
            errors["base"] = "invalid_mapping"
        else:
            return mapping_record(control, entity_id, label), False, errors

        return None, False, errors

    def _clear_page_configuration_state(self) -> None:
        """Clear the in-progress page editor after an atomic apply/remove."""
        self._selected_page = None
        self._page_handler = None
        self._page_controls = ()
        self._page_control_index = 0
        self._page_candidate = []

    def _accept_page_mapping(
        self,
        mapping: dict[str, object],
    ) -> bool:
        """Accept one draft mapping and atomically apply only a complete page."""
        manifest = self._runtime_manifest
        page = self._selected_page
        handler = self._page_handler
        if manifest is None or page is None or handler is None:
            raise MappingError("Page configuration state is unavailable")

        if handler.allow_partial_configuration:
            self._page_candidate = [
                current for current in self._page_candidate
                if current[CONF_CONTROL_SLOT] != mapping[CONF_CONTROL_SLOT]
            ]
            self._page_candidate.append(mapping)
            return False

        self._page_candidate.append(mapping)
        if self._page_control_index < len(self._page_controls) - 1:
            self._page_control_index += 1
            return False

        self._working_mappings = handler.replace_page_configuration(
            manifest,
            page,
            self._working_mappings,
            self._page_candidate,
        )
        self._clear_page_configuration_state()
        return True

    def _page_control_form(
        self,
        *,
        step_id: str,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        page = self._selected_page
        control = self._current_control()
        if page is None or control is None:
            raise MappingError("Page configuration state is unavailable")

        handler = self._page_handler
        if handler is None:
            raise MappingError("Page configuration handler is unavailable")

        existing = self._current_existing_mapping()
        entity_default = None
        label_default = None
        if existing is not None:
            entity = existing.get(CONF_ENTITY_ID)
            label = existing.get(CONF_LABEL)
            entity_default = entity if isinstance(entity, str) else None
            if handler.runtime_label_required:
                label_default = label if isinstance(label, str) else None

        return self.async_show_form(
            step_id=step_id,
            data_schema=build_mapping_schema(
                control,
                entity_default=entity_default,
                label_default=label_default,
                runtime_label_required=handler.runtime_label_required,
                allow_mapping_removal=(
                    handler.allow_mapping_removal and existing is not None
                ),
                removal_without_fields=handler.allow_partial_configuration,
            ),
            errors=errors or {},
            description_placeholders={
                "page_title": page.title,
                "template": page.template,
                "control_slot": str(control.control_slot),
                "function": control.function,
                "position": str(self._page_control_index + 1),
                "total": str(len(self._page_controls)),
            },
        )


class MatouchControllerConfigFlow(
    _PageConfigurationFlowMixin,
    ConfigFlow,
    domain=DOMAIN,
):
    """Handle MaTouch Controller configuration."""

    VERSION = 1

    def __init__(
        self,
        runtime_manifest: RuntimeManifest | None = None,
    ) -> None:
        super().__init__()
        self._runtime_manifest = runtime_manifest
        self._manifest_action: str | None = None
        self._working_mappings = []
        self._selected_page = None
        self._page_handler = None
        self._page_controls = ()
        self._page_control_index = 0
        self._page_candidate = []
        self._display_name = DEFAULT_DISPLAY_NAME

        self._reconfigure_base: tuple[dict[str, Any], str | None] | None = None
        self._reconfigure_manifest: RuntimeManifest | None = None
        self._reconfigure_mappings: list[dict[str, object]] = []
        self._reconfigure_page: PageManifest | None = None
        self._reconfigure_control: ControlManifest | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the Options Flow with separate device-local active-page admin."""
        # Local import avoids a module cycle: the DEV 3 extension subclasses the
        # stable DEV 2 page-first Options Flow defined later in this module.
        from .active_mask_options import MatouchControllerActiveMaskOptionsFlow

        return MatouchControllerActiveMaskOptionsFlow()

    async def _async_prepare_initial_flow(
        self,
        manifest: RuntimeManifest,
    ) -> ConfigFlowResult:
        """Validate identity and start with HA-local device metadata."""
        validate_runtime_manifest_templates(manifest)
        await self.async_set_unique_id(manifest.device_uid)
        if self._manifest_action is not None:
            existing = self.hass.config_entries.async_entry_for_domain_unique_id(
                DOMAIN, manifest.device_uid
            )
            # An ignored discovery entry has no MaTouch data; leave it to Core.
            if (
                existing is not None
                and effective_entry_data(existing).get(CONF_DEVICE_UID)
                == manifest.device_uid
            ):
                return self._async_reassociate_existing_entry(
                    existing, self._manifest_action
                )
        self._abort_if_unique_id_configured()
        self._initialize_page_configuration(manifest)
        return await self.async_step_device()

    @callback
    def _async_reassociate_existing_entry(
        self,
        entry: config_entries.ConfigEntry,
        manifest_action: str,
    ) -> ConfigFlowResult:
        """Point an existing entry for the same UID to the chosen action (HA §9.8 pt. 8).

        Only manifest_action changes; no new entry is created and mappings,
        display_name and revisions stay as they are. A loaded entry reloads
        through its update listener; an entry left in setup retry or setup
        error is reloaded explicitly, since it has no listener.
        """
        if effective_entry_data(entry).get(CONF_MANIFEST_ACTION) == manifest_action:
            return self.async_abort(reason="already_configured")

        persist_manifest_action(self.hass, entry, manifest_action)
        if entry.state in (
            config_entries.ConfigEntryState.SETUP_RETRY,
            config_entries.ConfigEntryState.SETUP_ERROR,
        ):
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_abort(reason="transport_reassociated")

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Select an ESPHome device and obtain its runtime manifest."""
        if self._runtime_manifest is not None:
            return await self._async_prepare_initial_flow(self._runtime_manifest)

        actions = available_manifest_actions(self.hass)
        if not actions:
            return self.async_abort(reason="manifest_unavailable")

        errors: dict[str, str] = {}
        if user_input is not None:
            action = user_input.get(CONF_MANIFEST_ACTION)
            try:
                manifest = await async_fetch_runtime_manifest(self.hass, action)
                validate_runtime_manifest_templates(manifest)
            except ManifestTransportUnavailableError:
                errors["base"] = "cannot_connect"
            except (ManifestTransportResponseError, ManifestError):
                errors["base"] = "invalid_manifest"
            else:
                self._manifest_action = action
                return await self._async_prepare_initial_flow(manifest)

        return self.async_show_form(
            step_id="user",
            data_schema=build_transport_schema(actions),
            errors=errors,
        )

    async def async_step_device(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Name the unit, then create its entry without pages (HA UX apply pt. 1)."""
        if self._runtime_manifest is None:
            return self.async_abort(reason="manifest_unavailable")

        errors: dict[str, str] = {}
        if user_input is not None:
            display_name, error = _validate_display_name(
                user_input.get(CONF_DISPLAY_NAME)
            )
            if error is not None:
                errors[CONF_DISPLAY_NAME] = error
            elif display_name is not None:
                self._display_name = display_name
                return self._async_create_initial_entry(self._runtime_manifest)

        return self.async_show_form(
            step_id="device",
            data_schema=build_display_name_schema(self._display_name),
            errors=errors,
        )

    @callback
    def _async_create_initial_entry(
        self,
        manifest: RuntimeManifest,
    ) -> ConfigFlowResult:
        """Create the entry with zero mappings; pages are configured in Options.

        Zero configured pages is valid (Core §2.2). Setup synchronizes the
        display at once and it shows unconfigured pages per Core §3.2.
        """
        entry_data: dict[str, Any] = {
            CONF_DEVICE_UID: manifest.device_uid,
            CONF_DISPLAY_NAME: self._display_name,
            CONF_CONFIG_REVISION: 1,
            CONF_VALIDATED_CONFIG_HASH: manifest.config_hash,
            CONF_MAPPINGS: [],
        }
        if self._manifest_action is not None:
            entry_data[CONF_MANIFEST_ACTION] = self._manifest_action

        return self.async_create_entry(
            title=self._display_name,
            data=entry_data,
        )

    async def _async_load_reconfigure_state(self) -> ConfigFlowResult | None:
        """Load current manifest and DEV 1-revalidated mappings."""
        if self._reconfigure_manifest is not None:
            return None

        entry = self._get_reconfigure_entry()
        base = _persistent_form_base(entry)
        effective = base[0]
        action = effective.get(CONF_MANIFEST_ACTION)
        if not isinstance(action, str) or not action:
            return self.async_abort(reason="manifest_unavailable")

        try:
            manifest = await async_fetch_runtime_manifest(self.hass, action)
            validate_runtime_manifest_templates(manifest)
        except ManifestTransportUnavailableError:
            return self.async_abort(reason="cannot_connect")
        except (ManifestTransportResponseError, ManifestError):
            return self.async_abort(reason="invalid_manifest")

        device_uid = effective.get(CONF_DEVICE_UID)
        if manifest.device_uid != device_uid:
            return self.async_abort(reason="invalid_manifest")

        await self.async_set_unique_id(manifest.device_uid)
        self._abort_if_unique_id_mismatch()

        try:
            mappings = validate_or_revalidate_entry_mappings(
                manifest,
                effective,
            )
        except MappingError:
            return self.async_abort(reason="invalid_mapping")

        self._reconfigure_base = base
        self._reconfigure_manifest = manifest
        self._reconfigure_mappings = mappings
        return None

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Select one functional page for point edit or configure-later."""
        if (abort := await self._async_load_reconfigure_state()) is not None:
            return abort

        manifest = self._reconfigure_manifest
        if manifest is None:
            return self.async_abort(reason="manifest_unavailable")

        errors: dict[str, str] = {}
        if user_input is not None:
            page_slots = {page.page_slot for page in manifest.pages}
            page_slot = _selected_slot(
                user_input.get(CONF_PAGE_SLOT),
                page_slots,
            )
            if page_slot is None:
                errors[CONF_PAGE_SLOT] = "invalid_selection"
            else:
                page = next(
                    page for page in manifest.pages if page.page_slot == page_slot
                )
                self._reconfigure_page = page
                self._reconfigure_control = None

                if (
                    page_is_configured(self._reconfigure_mappings, page)
                    or runtime_configuration_handler(page).allow_partial_configuration
                ):
                    try:
                        controls = runtime_configuration_handler(page).controls(page)
                    except ManifestError:
                        return self.async_abort(reason="invalid_manifest")
                    if not controls:
                        return self.async_abort(reason="invalid_manifest")
                    if len(controls) == 1:
                        self._reconfigure_control = controls[0]
                        return await self.async_step_reconfigure_mapping()
                    return await self.async_step_reconfigure_control()

                self._initialize_page_configuration(
                    manifest,
                    self._reconfigure_mappings,
                )
                try:
                    self._begin_page_configuration(page)
                except ManifestError:
                    return self.async_abort(reason="invalid_manifest")
                return await self.async_step_reconfigure_page_control()

        options = [
            _page_option(page, self._reconfigure_mappings)
            for page in manifest.pages
        ]

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_slot_selection_schema(CONF_PAGE_SLOT, options),
            errors=errors,
        )

    async def async_step_reconfigure_page_control(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Configure a previously unconfigured page atomically."""
        if self._selected_page is None:
            return await self.async_step_reconfigure()

        errors: dict[str, str] = {}
        if user_input is not None:
            mapping, remove_requested, errors = (
                self._validate_current_page_mapping(
                    self.hass,
                    user_input,
                )
            )
            if remove_requested:
                # An unconfigured atomic page offers no removal; keep the
                # guard without leaving the reconfigure flow.
                return await self.async_step_reconfigure()
            if mapping is not None:
                try:
                    completed = self._accept_page_mapping(mapping)
                except (ManifestError, MappingError):
                    errors["base"] = "invalid_mapping"
                else:
                    if completed:
                        return self._update_reconfigure_entry(
                            self._working_mappings
                        )
                    return await self.async_step_reconfigure_page_control()

        try:
            return self._page_control_form(
                step_id="reconfigure_page_control",
                errors=errors,
            )
        except MappingError:
            return self.async_abort(reason="invalid_mapping")

    async def async_step_reconfigure_control(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Select one control from an already configured page."""
        page = self._reconfigure_page
        if page is None:
            return await self.async_step_reconfigure()

        try:
            controls = runtime_configuration_handler(page).controls(page)
        except ManifestError:
            return self.async_abort(reason="invalid_manifest")

        errors: dict[str, str] = {}
        if user_input is not None:
            control_slots = {control.control_slot for control in controls}
            control_slot = _selected_slot(
                user_input.get(CONF_CONTROL_SLOT),
                control_slots,
            )
            if control_slot is None:
                errors[CONF_CONTROL_SLOT] = "invalid_selection"
            else:
                self._reconfigure_control = next(
                    control
                    for control in controls
                    if control.control_slot == control_slot
                )
                return await self.async_step_reconfigure_mapping()

        options: list[dict[str, str]] = []
        for control in controls:
            mapping = mapping_for_slots(
                self._reconfigure_mappings,
                page.page_slot,
                control.control_slot,
            )
            label = (
                mapping.get(CONF_LABEL)
                if mapping is not None
                and isinstance(mapping.get(CONF_LABEL), str)
                else (
                    mapping.get(CONF_ENTITY_ID)
                    if mapping is not None
                    and isinstance(mapping.get(CONF_ENTITY_ID), str)
                    else f"CONTROL{control.control_slot}"
                )
            )
            options.append(
                {
                    "value": str(control.control_slot),
                    "label": (
                        f"{control.control_slot}: {label} · {control.function}"
                    ),
                }
            )

        return self.async_show_form(
            step_id="reconfigure_control",
            data_schema=_slot_selection_schema(CONF_CONTROL_SLOT, options),
            errors=errors,
            description_placeholders={"page_title": page.title},
        )

    def _update_reconfigure_entry(
        self,
        mappings: list[dict[str, object]],
    ) -> ConfigFlowResult:
        """Persist one complete page/control reconfiguration."""
        manifest = self._reconfigure_manifest
        if manifest is None:
            return self.async_abort(reason="manifest_unavailable")

        entry = self._get_reconfigure_entry()
        effective = effective_entry_data(entry)
        revision = effective.get(CONF_CONFIG_REVISION, 0)
        if isinstance(revision, bool) or not isinstance(revision, int):
            revision = 0

        runtime_changed = mappings != self._reconfigure_mappings
        options = dict(entry.options)
        options.update(
            {
                CONF_CONFIG_REVISION: (
                    revision + 1 if runtime_changed else revision
                ),
                CONF_VALIDATED_CONFIG_HASH: manifest.config_hash,
                CONF_MAPPINGS: mappings,
            }
        )
        # Final comparison and Core's synchronous update are one operation.
        if self._reconfigure_base != _persistent_form_base(entry):
            return self.async_abort(reason="stale_configuration")
        return self.async_update_and_abort(
            entry,
            options=options,
        )

    async def async_step_reconfigure_mapping(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Edit one control while keeping its page persistently complete."""
        manifest = self._reconfigure_manifest
        page = self._reconfigure_page
        control = self._reconfigure_control
        if manifest is None or page is None or control is None:
            return await self.async_step_reconfigure()

        existing = mapping_for_slots(
            self._reconfigure_mappings,
            page.page_slot,
            control.control_slot,
        )
        try:
            handler = runtime_configuration_handler(page)
        except ManifestError:
            return self.async_abort(reason="invalid_manifest")

        if existing is None and not handler.allow_partial_configuration:
            return self.async_abort(reason="invalid_mapping")

        entity_default = existing.get(CONF_ENTITY_ID) if existing else None
        label_default = existing.get(CONF_LABEL) if existing else None
        if existing is not None and not isinstance(entity_default, str):
            return self.async_abort(reason="invalid_mapping")
        if (
            existing is not None
            and handler.runtime_label_required
            and not isinstance(label_default, str)
        ):
            return self.async_abort(reason="invalid_mapping")
        if not handler.runtime_label_required:
            label_default = None

        errors: dict[str, str] = {}
        if user_input is not None:
            if (
                handler.allow_mapping_removal
                and existing is not None
                and user_input.get(CONF_REMOVE_MAPPING) is True
            ):
                try:
                    candidate = []
                    if handler.allow_partial_configuration:
                        candidate = [
                            mapping for mapping in page_mappings(
                                self._reconfigure_mappings, page
                            )
                            if mapping[CONF_CONTROL_SLOT] != control.control_slot
                        ]
                    mappings = handler.replace_page_configuration(
                        manifest,
                        page,
                        self._reconfigure_mappings,
                        candidate,
                    )
                except (ManifestError, MappingError):
                    errors["base"] = "invalid_mapping"
                else:
                    return self._update_reconfigure_entry(mappings)

            seen_entities = {
                str(mapping[CONF_ENTITY_ID])
                for mapping in self._reconfigure_mappings
                if mapping[CONF_PAGE_SLOT] == page.page_slot
                and mapping[CONF_CONTROL_SLOT] != control.control_slot
                and isinstance(mapping.get(CONF_ENTITY_ID), str)
            }
            try:
                entity_id = validate_mapping_entity(
                    self.hass,
                    control,
                    user_input.get(CONF_ENTITY_ID),
                    seen_entities,
                )
                label = (
                    validate_runtime_label(user_input.get(CONF_LABEL))
                    if handler.runtime_label_required
                    else None
                )
            except RuntimeLabelError as err:
                errors[CONF_LABEL] = err.translation_key
            except MappingError:
                errors["base"] = "invalid_mapping"
            else:
                replacement = mapping_record(control, entity_id, label)
                current_page = page_mappings(
                    self._reconfigure_mappings,
                    page,
                )
                candidate = [
                    replacement
                    if mapping[CONF_CONTROL_SLOT] == control.control_slot
                    else dict(mapping)
                    for mapping in current_page
                ]
                if existing is None:
                    candidate.append(replacement)
                try:
                    mappings = handler.replace_page_configuration(
                        manifest,
                        page,
                        self._reconfigure_mappings,
                        candidate,
                    )
                except (ManifestError, MappingError):
                    errors["base"] = "invalid_mapping"
                else:
                    return self._update_reconfigure_entry(mappings)

        return self.async_show_form(
            step_id="reconfigure_mapping",
            data_schema=build_mapping_schema(
                control,
                entity_default=entity_default,
                label_default=label_default,
                runtime_label_required=handler.runtime_label_required,
                allow_mapping_removal=(
                    handler.allow_mapping_removal and existing is not None
                ),
                removal_without_fields=handler.allow_partial_configuration,
            ),
            errors=errors,
            description_placeholders={
                "page_title": page.title,
                "control_label": (
                    label_default
                    if isinstance(label_default, str)
                    else entity_default or f"CONTROL{control.control_slot}"
                ),
                "function": control.function,
            },
        )


class MatouchControllerOptionsFlow(
    _PageConfigurationFlowMixin,
    config_entries.OptionsFlow,
):
    """Persistent menu that saves each confirmed unit of edit (HA UX apply).

    One complete, valid and confirmed unit (pt. 3) is written at once and the
    menu stays open (pt. 2). Closing the dialog or "Done" abandons only the
    unconfirmed edit (pt. 5). Before each save the form base is compared with
    the persistent configuration without an await, and it is refreshed after
    each own save (pt. 6, §9.7).
    """

    def __init__(self) -> None:
        super().__init__()
        self._runtime_manifest = None
        self._working_mappings = []
        self._options_base: tuple[dict[str, Any], str | None] | None = None
        self._selected_page = None
        self._page_handler = None
        self._page_controls = ()
        self._page_control_index = 0
        self._page_candidate = []
        self._display_name = DEFAULT_DISPLAY_NAME
        self._saved_revision: int | None = None

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Refresh and validate the manifest, then open the menu."""
        base = _persistent_form_base(self.config_entry, include_title=True)
        effective = base[0]
        action = effective.get(CONF_MANIFEST_ACTION)
        if not isinstance(action, str) or not action:
            return self.async_abort(reason="manifest_unavailable")

        try:
            manifest = await async_fetch_runtime_manifest(self.hass, action)
            validate_runtime_manifest_templates(manifest)
        except ManifestTransportUnavailableError:
            return self.async_abort(reason="cannot_connect")
        except (ManifestTransportResponseError, ManifestError):
            return self.async_abort(reason="invalid_manifest")

        device_uid = effective.get(CONF_DEVICE_UID)
        if manifest.device_uid != device_uid:
            return self.async_abort(reason="invalid_manifest")

        try:
            mappings = validate_or_revalidate_entry_mappings(
                manifest,
                effective,
            )
        except MappingError:
            return self.async_abort(reason="invalid_mapping")

        configured_name = effective.get(CONF_DISPLAY_NAME)
        if isinstance(configured_name, str) and configured_name.strip():
            self._display_name = configured_name.strip()
        else:
            entry_title = getattr(self.config_entry, "title", "")
            if (
                isinstance(entry_title, str)
                and entry_title.strip()
                and entry_title != device_uid
            ):
                self._display_name = entry_title.strip()
            else:
                self._display_name = DEFAULT_DISPLAY_NAME

        self._options_base = base
        self._initialize_page_configuration(manifest, mappings)
        return await self.async_step_page()

    def _extra_menu_options(self) -> list[dict[str, str]]:
        """Return device-local administration items; extended by the subclass."""
        return []

    def _menu_options(self) -> list[dict[str, str]]:
        manifest = self._runtime_manifest
        if manifest is None:
            return []
        options = [
            _page_option(page, self._working_mappings)
            for page in manifest.pages
        ]
        options.append({"value": MENU_DISPLAY_NAME, "label": "Device name"})
        options.extend(self._extra_menu_options())
        options.append({"value": MENU_DONE, "label": "Done"})
        return options

    def _menu_form(
        self,
        *,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Show the menu; right after an own save it carries the confirmation."""
        saved_revision = self._saved_revision
        self._saved_revision = None
        schema = _slot_selection_schema(
            CONF_PAGE_SLOT,
            self._menu_options(),
            translation_key=MENU_TRANSLATION_KEY,
        )
        if saved_revision is not None and not errors:
            return self.async_show_form(
                step_id="page_saved",
                data_schema=schema,
                description_placeholders={
                    "config_revision": str(saved_revision),
                },
            )
        return self.async_show_form(
            step_id="page",
            data_schema=schema,
            errors=errors or {},
        )

    async def async_step_page(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Route one menu item; the menu stays open after each save."""
        manifest = self._runtime_manifest
        if manifest is None:
            return self.async_abort(reason="manifest_unavailable")

        if user_input is None:
            return self._menu_form()

        selection = user_input.get(CONF_PAGE_SLOT)
        if selection == MENU_DONE:
            # "Done" closes the flow without a write (pt. 2).
            return self.async_abort(reason="menu_closed")
        if selection == MENU_DISPLAY_NAME:
            return await self.async_step_device()

        page_slot = _selected_slot(
            selection,
            {page.page_slot for page in manifest.pages},
        )
        if page_slot is None:
            return self._menu_form(errors={CONF_PAGE_SLOT: "invalid_selection"})

        page = next(page for page in manifest.pages if page.page_slot == page_slot)
        try:
            self._begin_page_configuration(page)
        except ManifestError:
            return self.async_abort(reason="invalid_manifest")
        if self._page_handler.allow_partial_configuration:
            return await self.async_step_page_control_select()
        return await self.async_step_page_control()

    async def async_step_page_saved(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the menu shown with the save confirmation."""
        return await self.async_step_page(user_input)

    async def async_step_device(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Edit and save the Home Assistant-local display name (pt. 3)."""
        if self._runtime_manifest is None:
            return self.async_abort(reason="manifest_unavailable")

        errors: dict[str, str] = {}
        if user_input is not None:
            display_name, error = _validate_display_name(
                user_input.get(CONF_DISPLAY_NAME)
            )
            if error is not None:
                errors[CONF_DISPLAY_NAME] = error
            elif display_name is not None:
                return self._async_save_unit(
                    self._working_mappings,
                    display_name,
                )

        return self.async_show_form(
            step_id="device",
            data_schema=build_display_name_schema(self._display_name),
            errors=errors,
        )

    async def async_step_page_control_select(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose one optional slot to add, replace, rename or remove."""
        page = self._selected_page
        if page is None or self._page_handler is None:
            return self._menu_form()

        errors: dict[str, str] = {}
        if user_input is not None:
            selection = user_input.get(CONF_CONTROL_SLOT)
            if selection == SLOT_MENU_BACK:
                self._clear_page_configuration_state()
                return self._menu_form()
            slot = _selected_slot(
                selection,
                {control.control_slot for control in self._page_controls},
            )
            if slot is None:
                errors[CONF_CONTROL_SLOT] = "invalid_selection"
            else:
                self._page_control_index = next(
                    index
                    for index, control in enumerate(self._page_controls)
                    if control.control_slot == slot
                )
                return await self.async_step_page_control()

        options = []
        for control in self._page_controls:
            mapping = mapping_for_slots(
                self._page_candidate,
                page.page_slot,
                control.control_slot,
            )
            label = (
                mapping[CONF_LABEL]
                if mapping is not None and isinstance(mapping.get(CONF_LABEL), str)
                else UNCONFIGURED_MARK
            )
            options.append(
                {
                    "value": str(control.control_slot),
                    "label": f"{control.control_slot}: {label}",
                }
            )
        options.append({"value": SLOT_MENU_BACK, "label": "Back to menu"})
        return self.async_show_form(
            step_id="page_control_select",
            data_schema=_slot_selection_schema(
                CONF_CONTROL_SLOT,
                options,
                translation_key=SLOT_MENU_TRANSLATION_KEY,
            ),
            errors=errors,
            description_placeholders={"page_title": page.title},
        )

    def _page_unit_candidate(
        self,
        mapping: dict[str, object] | None,
    ) -> list[dict[str, object]] | None:
        """Return all mappings after one confirmed unit, or None for a draft.

        `mapping=None` is an explicit removal. The result is validated by the
        template handler; an incomplete Lights 1.0 page stays a draft.
        """
        manifest = self._runtime_manifest
        page = self._selected_page
        handler = self._page_handler
        control = self._current_control()
        if manifest is None or page is None or handler is None or control is None:
            raise MappingError("Page configuration state is unavailable")

        if handler.allow_partial_configuration:
            # Lights 1.1 / Scene: one slot is the unit (§8.4, §8C.2).
            candidate = [
                dict(current)
                for current in self._page_candidate
                if current[CONF_CONTROL_SLOT] != control.control_slot
            ]
            if mapping is not None:
                candidate.append(mapping)
            return handler.replace_page_configuration(
                manifest, page, self._working_mappings, candidate
            )

        if mapping is None:
            # Climate / Dehumidifier: explicit 1/1 -> 0/1 removal.
            return handler.replace_page_configuration(
                manifest, page, self._working_mappings, []
            )

        # The atomic page is the unit; never write its incomplete draft.
        draft = [*self._page_candidate, mapping]
        if len(draft) < len(self._page_controls):
            self._page_candidate = draft
            self._page_control_index += 1
            return None
        return handler.replace_page_configuration(
            manifest, page, self._working_mappings, draft
        )

    async def async_step_page_control(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Edit one control and save as soon as the unit is complete."""
        if self._selected_page is None:
            return self._menu_form()

        errors: dict[str, str] = {}
        if user_input is not None:
            mapping, remove_requested, errors = (
                self._validate_current_page_mapping(
                    self.hass,
                    user_input,
                )
            )
            if remove_requested or mapping is not None:
                try:
                    mappings = self._page_unit_candidate(
                        None if remove_requested else mapping
                    )
                except (ManifestError, MappingError):
                    errors["base"] = "invalid_mapping"
                else:
                    if mappings is None:
                        return await self.async_step_page_control()
                    return self._async_save_unit(mappings, self._display_name)

        try:
            return self._page_control_form(
                step_id="page_control",
                errors=errors,
            )
        except MappingError:
            return self.async_abort(reason="invalid_mapping")

    @callback
    def _async_save_unit(
        self,
        mappings: list[dict[str, object]],
        display_name: str,
    ) -> ConfigFlowResult:
        """Persist one confirmed unit of edit and return to the menu.

        The stale check and Core's synchronous entry update run without an
        await in between. A real no-op writes nothing; a mapping change bumps
        the revision once and the existing listener reloads and resyncs; a
        display_name-only change keeps the revision and is not resynced.
        """
        manifest = self._runtime_manifest
        if manifest is None:
            return self.async_abort(reason="manifest_unavailable")

        entry = self.config_entry
        if self._options_base != _persistent_form_base(entry, include_title=True):
            return self.async_abort(reason="stale_configuration")

        effective = effective_entry_data(entry)
        revision = effective.get(CONF_CONFIG_REVISION, 0)
        if isinstance(revision, bool) or not isinstance(revision, int):
            revision = 0

        runtime_changed = mappings != self._working_mappings
        updates: dict[str, Any] = {
            CONF_DISPLAY_NAME: display_name,
            CONF_CONFIG_REVISION: revision + 1 if runtime_changed else revision,
            CONF_VALIDATED_CONFIG_HASH: manifest.config_hash,
            CONF_MAPPINGS: [dict(mapping) for mapping in mappings],
        }
        self._clear_page_configuration_state()
        if all(effective.get(key) == value for key, value in updates.items()):
            return self._menu_form()

        options = dict(entry.options)
        options.update(updates)
        # The title is written with the options, so the listener's title sync
        # does not change the refreshed base afterwards.
        self.hass.config_entries.async_update_entry(
            entry,
            options=options,
            title=display_name,
        )
        self._working_mappings = [dict(mapping) for mapping in mappings]
        self._display_name = display_name
        self._options_base = _persistent_form_base(entry, include_title=True)
        self._saved_revision = updates[CONF_CONFIG_REVISION]
        return self._menu_form()
