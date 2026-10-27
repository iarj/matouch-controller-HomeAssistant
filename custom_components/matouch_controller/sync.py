"""Home Assistant -> MaTouch display runtime synchronization."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import time
from typing import Any
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ENTITY_ID, EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from .auxiliary import resolve_auxiliary_runtime_values
from .const import (
    CONF_CONFIG_REVISION,
    CONF_CONTROL_SLOT,
    CONF_LABEL,
    CONF_MANIFEST_ACTION,
    CONF_PAGE_SLOT,
    DOMAIN,
    page_device_identifier,
)
from .manifest_model import (
    ControlManifest,
    ManifestError,
    RuntimeManifest,
    normalize_functional_page_header,
)
from .mapping import control_for_slots, effective_entry_data, mapping_for_slots

ESPHOME_DOMAIN = "esphome"
MANIFEST_ACTION_SUFFIX = "_matouch_get_manifest"
CONFIGURATION_STATE_SUFFIX = "_matouch_set_configuration_state"
BEGIN_SNAPSHOT_SUFFIX = "_matouch_begin_snapshot"
CONTROL_STATE_SUFFIX = "_matouch_set_control_state"
END_SNAPSHOT_SUFFIX = "_matouch_end_snapshot"

ERROR_OK = "OK"
ERROR_ENTITY_MISSING = "E_ENTITY_MISSING"
ERROR_ENTITY_UNAVAILABLE = "E_ENTITY_UNAVAILABLE"

_COMMAND_CORRELATION_TTL_SECONDS = 10.0


class DisplaySyncError(HomeAssistantError):
    """Base error for HA -> display synchronization failures."""


class DisplaySyncUnavailableError(DisplaySyncError):
    """Raised when the ESPHome display synchronization actions are unavailable."""


def _split_manifest_action(action: Any) -> tuple[str, str]:
    """Return ESPHome domain and device service prefix from a manifest action."""
    if not isinstance(action, str):
        raise DisplaySyncUnavailableError("Manifest action must be a string")

    domain, separator, service = action.partition(".")
    if (
        separator != "."
        or domain != ESPHOME_DOMAIN
        or not service.endswith(MANIFEST_ACTION_SUFFIX)
    ):
        raise DisplaySyncUnavailableError(f"Invalid manifest action: {action}")

    return domain, service[: -len(MANIFEST_ACTION_SUFFIX)]


def display_action_names(manifest_action: str) -> dict[str, tuple[str, str]]:
    """Derive the HA service names for the ESPHome display actions."""
    domain, prefix = _split_manifest_action(manifest_action)
    return {
        "configuration": (domain, f"{prefix}{CONFIGURATION_STATE_SUFFIX}"),
        "begin_snapshot": (domain, f"{prefix}{BEGIN_SNAPSHOT_SUFFIX}"),
        "control_state": (domain, f"{prefix}{CONTROL_STATE_SUFFIX}"),
        "end_snapshot": (domain, f"{prefix}{END_SNAPSHOT_SUFFIX}"),
    }


def scene_launcher_availability(
    entity_id: str, state: State | None
) -> tuple[bool, str]:
    """Resolve launch availability without interpreting a functional state."""
    if state is None:
        return False, ERROR_ENTITY_MISSING
    domain = entity_id.split(".", 1)[0]
    if domain == "scene" and state.state != "unavailable":
        return True, ERROR_OK
    if domain == "script" and state.state in {"on", "off"}:
        return True, ERROR_OK
    return False, ERROR_ENTITY_UNAVAILABLE


def _binary_state_payload(state: State | None) -> tuple[bool, bool, str]:
    """Return available/state/error for one binary HA entity state."""
    if state is None:
        return False, False, ERROR_ENTITY_MISSING
    if state.state in {"unknown", "unavailable"}:
        return False, False, ERROR_ENTITY_UNAVAILABLE
    return True, state.state == "on", ERROR_OK


def _auxiliary_state_payload(
    control: ControlManifest,
    entity_id: str,
    state: State | None,
    *,
    template: str | None = None,
    template_version: str | None = None,
    temperature_unit: str | None = None,
) -> dict[str, list[Any]]:
    """Return aligned positional arrays for declared auxiliary runtime values."""
    values = resolve_auxiliary_runtime_values(
        control,
        entity_id,
        state,
        template=template,
        template_version=template_version,
        temperature_unit=temperature_unit,
    )
    return {
        "value_keys": [value.key for value in values],
        "value_kinds": [value.kind for value in values],
        "value_supported": [value.supported for value in values],
        "value_available": [value.available for value in values],
        "value_numbers": [value.number for value in values],
        "value_texts": [value.text for value in values],
    }


def _unmapped_auxiliary_state_payload(
    control: ControlManifest,
) -> dict[str, list[Any]]:
    """Return declared auxiliary keys with neutral runtime values for unmapped controls."""
    return {
        "value_keys": [value.key for value in control.auxiliary_values],
        "value_kinds": [value.kind for value in control.auxiliary_values],
        "value_supported": [False for _ in control.auxiliary_values],
        "value_available": [False for _ in control.auxiliary_values],
        "value_numbers": [0.0 for _ in control.auxiliary_values],
        "value_texts": ["" for _ in control.auxiliary_values],
    }


def build_control_state_payload(
    *,
    integration_session_id: str,
    config_revision: int,
    state_revision: int,
    mapping: Mapping[str, Any],
    control: ControlManifest,
    state: State | None,
    request_id: str = "",
    template: str | None = None,
    template_version: str | None = None,
    temperature_unit: str | None = None,
) -> dict[str, Any]:
    """Build one normative mapped matouch_set_control_state payload."""
    available = False
    state_bool = False
    state_number = 0.0
    state_text = ""
    error_code = ERROR_OK

    if control.function == "press" and control.state_kind == "none":
        available, error_code = scene_launcher_availability(
            mapping[CONF_ENTITY_ID], state
        )
        # Availability/state events are never execution confirmations.
        request_id = ""
    elif control.state_kind == "binary":
        available, state_bool, error_code = _binary_state_payload(state)
    elif state is None:
        error_code = ERROR_ENTITY_MISSING
    elif state.state in {"unknown", "unavailable"}:
        error_code = ERROR_ENTITY_UNAVAILABLE
    else:
        available = True
        if control.state_kind == "number":
            try:
                state_number = float(state.state)
            except (TypeError, ValueError):
                available = False
                error_code = ERROR_ENTITY_UNAVAILABLE
        elif control.state_kind in {"text", "option"}:
            state_text = state.state

    entity_id = mapping[CONF_ENTITY_ID]
    auxiliary = _auxiliary_state_payload(
        control,
        entity_id,
        state,
        template=template,
        template_version=template_version,
        temperature_unit=temperature_unit,
    )
    return {
        "integration_session_id": integration_session_id,
        "config_revision": config_revision,
        "state_revision": state_revision,
        "page_slot": mapping[CONF_PAGE_SLOT],
        "control_slot": mapping[CONF_CONTROL_SLOT],
        "label": mapping.get(CONF_LABEL, ""),
        "mapped": True,
        "available": available,
        "state_kind": control.state_kind,
        "state_bool": state_bool,
        "state_number": state_number,
        "state_text": state_text,
        "attribute_text": "",
        **auxiliary,
        "request_id": request_id,
        "error_code": error_code,
    }


def build_unmapped_control_state_payload(
    *,
    integration_session_id: str,
    config_revision: int,
    state_revision: int,
    control: ControlManifest,
) -> dict[str, Any]:
    """Build the full-snapshot state for one compiled but unmapped control."""
    auxiliary = _unmapped_auxiliary_state_payload(control)
    return {
        "integration_session_id": integration_session_id,
        "config_revision": config_revision,
        "state_revision": state_revision,
        "page_slot": control.page_slot,
        "control_slot": control.control_slot,
        "label": "",
        "mapped": False,
        "available": False,
        "state_kind": control.state_kind,
        "state_bool": False,
        "state_number": 0.0,
        "state_text": "",
        "attribute_text": "",
        **auxiliary,
        "request_id": "",
        "error_code": ERROR_OK,
    }


@dataclass
class EntryDisplaySync:
    """Synchronize one loaded MaTouch Config Entry to its display."""

    hass: HomeAssistant
    entry: ConfigEntry
    manifest: RuntimeManifest
    mappings: list[dict[str, Any]]
    integration_session_id: str = field(default_factory=lambda: uuid4().hex)
    state_revision: int = 0
    active_boot_id: str | None = None
    _unsubscribe_state: Callable[[], None] | None = None
    _actions: dict[str, tuple[str, str]] = field(init=False)
    _pending_snapshot: tuple[str, int, int] | None = None
    _pending_entities: dict[str, str] = field(default_factory=dict)
    _command_correlations: dict[str, tuple[str, str, float]] = field(
        default_factory=dict
    )
    _transport_generation: int = 0
    _awaiting_handshake: bool = False
    _stopped: bool = False
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        effective = effective_entry_data(self.entry)
        action = effective.get(CONF_MANIFEST_ACTION)
        self._actions = display_action_names(action)

    def _page_title_payload(self) -> tuple[list[int], list[str]]:
        """Return page titles from HA child-device names with compiled fallback."""
        registry = None
        try:
            registry = dr.async_get(self.hass)
        except (KeyError, RuntimeError, AttributeError, TypeError):
            pass

        entry_id = getattr(self.entry, "entry_id", None)
        page_slots: list[int] = []
        page_titles: list[str] = []
        for page in self.manifest.pages:
            title = page.title
            if registry is not None and isinstance(entry_id, str) and entry_id:
                device = registry.async_get_child_device_by_identifier(
                    (DOMAIN, page_device_identifier(self.manifest.device_uid, page.page_slot)),
                    entry_id,
                )
                if device is not None:
                    for candidate in (device.name_by_user, device.name):
                        try:
                            title = normalize_functional_page_header(
                                candidate,
                                f"page header candidate for slot {page.page_slot}",
                            )
                        except ManifestError:
                            continue
                        break
            page_slots.append(page.page_slot)
            page_titles.append(title)
        return page_slots, page_titles

    @property
    def config_revision(self) -> int:
        """Return the persistent runtime configuration revision."""
        revision = effective_entry_data(self.entry).get(CONF_CONFIG_REVISION)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise DisplaySyncError("Invalid config_revision")
        return revision

    def _require_lifecycle_active(self) -> None:
        """Reject normal-runtime work after this sync lifecycle was invalidated."""
        if self._stopped:
            raise DisplaySyncError("Display synchronization lifecycle is stopped")

    def begin_device_handshake(self, boot_id: str) -> int:
        """Start a reconnect boundary for the device boot that sent hello.

        A fresh hello invalidates any incomplete snapshot and command correlation
        from the previous transport connection. The Home Assistant integration
        session and its monotonic state revision intentionally remain unchanged.
        The returned transport generation fences any older scheduled resync task.
        """
        self._require_lifecycle_active()
        if not isinstance(boot_id, str) or not boot_id:
            raise DisplaySyncError("Invalid device boot identity")
        self._transport_generation += 1
        self.active_boot_id = boot_id
        self._awaiting_handshake = True
        self._pending_snapshot = None
        self._pending_entities.clear()
        self._command_correlations.clear()
        return self._transport_generation

    def require_active_boot(self, boot_id: str) -> None:
        """Reject a device-originated message outside the active hello boot."""
        self._require_lifecycle_active()
        if self.active_boot_id is None:
            raise DisplaySyncError("Device boot identity is not established")
        if boot_id != self.active_boot_id:
            raise DisplaySyncError("boot_id does not match active device boot")

    def require_command_ready(self, boot_id: str) -> None:
        """Reject commands until the active boot has completed snapshot ACK."""
        self.require_active_boot(boot_id)
        if self._awaiting_handshake or self._pending_snapshot is not None:
            raise DisplaySyncError("Display synchronization is not ready for commands")

    def _require_transport_generation(self, expected_generation: int) -> None:
        """Stop snapshot work scheduled by an older hello or stopped lifecycle."""
        self._require_lifecycle_active()
        if expected_generation != self._transport_generation:
            raise DisplaySyncError("Reconnect snapshot superseded by newer hello")

    def _require_publication(self, expected_generation: int) -> None:
        """Fence every positive publication on accepted boot and lifecycle."""
        self._require_transport_generation(expected_generation)
        if self.active_boot_id is None:
            raise DisplaySyncError("Device boot identity is not established")

    def _next_state_revision(self) -> int:
        """Advance the unsigned 32-bit state revision within this session."""
        if self.state_revision >= 0xFFFFFFFF:
            self.integration_session_id = uuid4().hex
            self.state_revision = 0
        self.state_revision += 1
        return self.state_revision

    def _prune_command_correlations(self) -> None:
        """Discard command correlations outside the confirmation window."""
        now = time.monotonic()
        self._command_correlations = {
            context_id: correlation
            for context_id, correlation in self._command_correlations.items()
            if correlation[2] > now
        }

    def register_command_correlation(
        self,
        *,
        context_id: str,
        entity_id: str,
        request_id: str,
    ) -> None:
        """Register one deterministic HA-context command correlation."""
        self._require_lifecycle_active()
        self._prune_command_correlations()
        self._command_correlations[context_id] = (
            entity_id,
            request_id,
            time.monotonic() + _COMMAND_CORRELATION_TTL_SECONDS,
        )

    def clear_command_correlation(
        self,
        *,
        context_id: str,
        request_id: str,
    ) -> None:
        """Clear a correlation when command execution fails."""
        correlation = self._command_correlations.get(context_id)
        if correlation is not None and correlation[1] == request_id:
            self._command_correlations.pop(context_id, None)

    def _request_id_for_state_event(self, event: Event, entity_id: str) -> str:
        """Return and consume request_id for a causally matching HA event."""
        self._prune_command_correlations()
        context = getattr(event, "context", None)
        context_id = getattr(context, "id", None)
        parent_id = getattr(context, "parent_id", None)

        for candidate in (context_id, parent_id):
            if not isinstance(candidate, str):
                continue
            correlation = self._command_correlations.get(candidate)
            if correlation is None or correlation[0] != entity_id:
                continue
            self._command_correlations.pop(candidate, None)
            return correlation[1]

        return ""

    async def _async_call_action(
        self,
        name: str,
        data: dict[str, Any],
        *,
        allow_stopped: bool = False,
    ) -> None:
        if not allow_stopped:
            self._require_lifecycle_active()
        domain, service = self._actions[name]
        try:
            await self.hass.services.async_call(
                domain,
                service,
                data,
                blocking=True,
            )
        except HomeAssistantError as err:
            raise DisplaySyncUnavailableError(
                f"ESPHome display action failed: {domain}.{service}"
            ) from err
        if not allow_stopped:
            self._require_lifecycle_active()

    def _control_for_mapping(self, mapping: Mapping[str, Any]) -> ControlManifest:
        control = control_for_slots(
            self.manifest,
            mapping[CONF_PAGE_SLOT],
            mapping[CONF_CONTROL_SLOT],
        )
        if control is None:
            raise DisplaySyncError("Mapping control missing from active manifest")
        return control

    def _projection_context(
        self,
        control: ControlManifest,
    ) -> tuple[str, str, str | None]:
        """Return template/version and HA temperature unit for state projection."""
        for page in self.manifest.pages:
            if page.page_slot == control.page_slot:
                config = getattr(self.hass, "config", None)
                units = getattr(config, "units", None)
                raw_unit = getattr(units, "temperature_unit", None)
                raw_unit = getattr(raw_unit, "value", raw_unit)
                temperature_unit = (
                    raw_unit if isinstance(raw_unit, str) and raw_unit else None
                )
                return page.template, page.template_version, temperature_unit
        raise DisplaySyncError("Control page missing from active manifest")

    async def async_send_configuration_state(
        self,
        *,
        compatible: bool,
        configured: bool,
        error_code: str = ERROR_OK,
        message: str = "",
    ) -> None:
        """Send the current normal-runtime configuration state."""
        generation = self._transport_generation
        async with self._send_lock:
            self._require_transport_generation(generation)
            await self._async_send_configuration_state_locked(
                compatible=compatible, configured=configured,
                error_code=error_code, message=message,
            )
            self._require_transport_generation(generation)

    async def _async_send_configuration_state_locked(
        self, *, compatible: bool, configured: bool,
        error_code: str = ERROR_OK, message: str = "",
    ) -> None:
        """Publish configuration while the caller owns snapshot serialization."""
        self._require_lifecycle_active()
        if compatible and configured:
            self._require_publication(self._transport_generation)
        if not compatible or not configured:
            self._pending_snapshot = None
            self._pending_entities.clear()
            self._command_correlations.clear()

        page_slots, page_titles = self._page_title_payload()
        await self._async_call_action(
            "configuration",
            {
                "integration_session_id": self.integration_session_id,
                "compatible": compatible,
                "configured": configured,
                "config_revision": self.config_revision,
                "page_slots": page_slots,
                "page_titles": page_titles,
                "error_code": error_code,
                "message": message,
            },
        )

    async def async_send_shutdown_configuration_state(
        self,
        *,
        error_code: str = "E_NOT_CONFIGURED",
        message: str = "MaTouch integration runtime unavailable",
    ) -> None:
        """Send the one shutdown-only configuration notice after invalidation."""
        if not self._stopped:
            raise DisplaySyncError(
                "Shutdown configuration state requires an invalidated lifecycle"
            )
        page_slots, page_titles = self._page_title_payload()
        await self._async_call_action(
            "configuration",
            {
                "integration_session_id": self.integration_session_id,
                "compatible": True,
                "configured": False,
                "config_revision": self.config_revision,
                "page_slots": page_slots,
                "page_titles": page_titles,
                "error_code": error_code,
                "message": message,
            },
            allow_stopped=True,
        )

    async def async_send_snapshot(
        self,
        *,
        transport_generation: int | None = None,
    ) -> None:
        """Serialize one complete snapshot, releasing the lock before ACK."""
        if transport_generation is None:
            transport_generation = self._transport_generation
        async with self._send_lock:
            self._require_publication(transport_generation)
            await self._async_send_snapshot_locked(transport_generation)
        await self._async_flush_pending_entities(transport_generation)

    async def async_recover_snapshot(
        self, *, integration_session_id: str,
        config_revision: int, state_revision: int,
    ) -> None:
        """Check and replace timeout identity under the same send lock."""
        generation = self._transport_generation
        received = (integration_session_id, config_revision, state_revision)
        async with self._send_lock:
            self._require_publication(generation)
            expected = self._pending_snapshot
            if expected is None or received != expected:
                raise DisplaySyncError(
                    "Snapshot timeout request does not match pending snapshot"
                )
            self._command_correlations.clear()
            try:
                await self._async_send_snapshot_locked(generation)
            except (DisplaySyncError, asyncio.CancelledError):
                if (
                    not self._stopped
                    and self._transport_generation == generation
                    and self._pending_snapshot is None
                ):
                    # Retryable timeout identity, never an ACK authority.
                    self._pending_snapshot = expected
                raise
        await self._async_flush_pending_entities(generation)

    async def _async_send_snapshot_locked(self, transport_generation: int) -> None:
        """Send configuration/begin/controls/end with one owner."""
        self._require_publication(transport_generation)

        # Cover configuration transport too: no old ACK or incremental may
        # escape between invalidating a snapshot and starting its replacement.
        self._awaiting_handshake = True
        self._pending_snapshot = None
        revision = self.config_revision
        await self._async_send_configuration_state_locked(
            compatible=True,
            configured=True,
        )
        self._require_transport_generation(transport_generation)

        state_revision = self._next_state_revision()
        self._pending_snapshot = (
            self.integration_session_id,
            revision,
            state_revision,
        )
        # The pending tuple now owns the barrier until its matching ACK.
        self._awaiting_handshake = False
        await self._async_call_action(
            "begin_snapshot",
            {
                "integration_session_id": self.integration_session_id,
                "config_revision": revision,
                "state_revision": state_revision,
                "control_count": len(self.manifest.controls),
            },
        )
        self._require_transport_generation(transport_generation)

        for control in self.manifest.controls:
            mapping = mapping_for_slots(
                self.mappings,
                control.page_slot,
                control.control_slot,
            )
            if mapping is None:
                payload = build_unmapped_control_state_payload(
                    integration_session_id=self.integration_session_id,
                    config_revision=revision,
                    state_revision=state_revision,
                    control=control,
                )
            else:
                entity_id = mapping[CONF_ENTITY_ID]
                template, template_version, temperature_unit = (
                    self._projection_context(control)
                )
                payload = build_control_state_payload(
                    integration_session_id=self.integration_session_id,
                    config_revision=revision,
                    state_revision=state_revision,
                    mapping=mapping,
                    control=control,
                    state=self.hass.states.get(entity_id),
                    template=template,
                    template_version=template_version,
                    temperature_unit=temperature_unit,
                )
            await self._async_call_action("control_state", payload)
            self._require_transport_generation(transport_generation)

        await self._async_call_action(
            "end_snapshot",
            {
                "integration_session_id": self.integration_session_id,
                "config_revision": revision,
                "state_revision": state_revision,
            },
        )
        self._require_publication(transport_generation)

    async def async_push_entity_state(
        self,
        entity_id: str,
        *,
        request_id: str = "",
    ) -> None:
        """Push one HA entity state to every mapped display control."""
        self._require_lifecycle_active()
        targets = [
            mapping
            for mapping in self.mappings
            if mapping.get(CONF_ENTITY_ID) == entity_id
        ]
        if not targets:
            return
        if (
            self.active_boot_id is None
            or self._send_lock.locked()
            or self._awaiting_handshake
            or self._pending_snapshot is not None
        ):
            self._pending_entities[entity_id] = request_id
            return

        transport_generation = self._transport_generation
        self._require_publication(transport_generation)
        state_revision = self._next_state_revision()
        state = self.hass.states.get(entity_id)
        for mapping in targets:
            self._require_lifecycle_active()
            if transport_generation != self._transport_generation:
                return
            if self._awaiting_handshake or self._pending_snapshot is not None:
                self._pending_entities[entity_id] = request_id
                return
            control = self._control_for_mapping(mapping)
            template, template_version, temperature_unit = self._projection_context(
                control
            )
            payload = build_control_state_payload(
                integration_session_id=self.integration_session_id,
                config_revision=self.config_revision,
                state_revision=state_revision,
                mapping=mapping,
                control=control,
                state=state,
                request_id=request_id,
                template=template,
                template_version=template_version,
                temperature_unit=temperature_unit,
            )
            await self._async_call_action("control_state", payload)
            self._require_lifecycle_active()
            if transport_generation != self._transport_generation:
                return

    async def async_ack_snapshot(
        self,
        *,
        integration_session_id: str,
        config_revision: int,
        state_revision: int,
    ) -> None:
        """Accept the matching snapshot ACK and release incremental updates."""
        self._require_lifecycle_active()
        expected = self._pending_snapshot
        received = (
            integration_session_id,
            config_revision,
            state_revision,
        )
        if self._awaiting_handshake or expected is None or received != expected:
            raise DisplaySyncError("Snapshot ACK does not match pending snapshot")

        transport_generation = self._transport_generation
        self._pending_snapshot = None
        await self._async_flush_pending_entities(transport_generation)

    async def _async_flush_pending_entities(self, transport_generation: int) -> None:
        """Flush after ACK/end without waiting for transport under the send lock."""
        while self._pending_entities:
            self._require_lifecycle_active()
            if transport_generation != self._transport_generation:
                return
            if (
                self._send_lock.locked()
                or self._awaiting_handshake
                or self._pending_snapshot is not None
                or self.active_boot_id is None
            ):
                return
            entity_id = min(self._pending_entities)
            request_id = self._pending_entities.pop(entity_id)
            try:
                await self.async_push_entity_state(entity_id, request_id=request_id)
            except (DisplaySyncError, asyncio.CancelledError):
                if not self._stopped and transport_generation == self._transport_generation:
                    self._pending_entities.setdefault(entity_id, request_id)
                raise

    async def _async_state_changed(self, event: Event) -> None:
        if self._stopped:
            return
        entity_id = event.data.get("entity_id")
        if isinstance(entity_id, str):
            request_id = self._request_id_for_state_event(event, entity_id)
            await self.async_push_entity_state(
                entity_id,
                request_id=request_id,
            )

    async def async_start(self, *, send_snapshot: bool = True) -> None:
        """Subscribe to HA state and optionally send an initial snapshot."""
        self._require_lifecycle_active()
        generation = self._transport_generation
        self._awaiting_handshake = True
        self._unsubscribe_state = self.hass.bus.async_listen(
            EVENT_STATE_CHANGED,
            self._async_state_changed,
        )
        try:
            if send_snapshot:
                await self.async_send_snapshot(transport_generation=generation)
        except DisplaySyncError:
            if not self._stopped and generation != self._transport_generation:
                # Accepted hello now owns bootstrap on this same live sync.
                # Stopping it would invalidate the newer queued resync too.
                return
            self.invalidate()
            raise
        except BaseException:
            # Startup failure/cancellation must not leak the early subscription
            # or leave an incomplete snapshot eligible for acknowledgement.
            self._awaiting_handshake = True
            self._pending_snapshot = None
            await self.async_stop()
            raise

    def invalidate(self) -> None:
        """Synchronously and permanently invalidate this runtime lifecycle."""
        if self._stopped:
            return

        self._stopped = True
        self._transport_generation += 1
        self.active_boot_id = None
        self._awaiting_handshake = True
        self._pending_snapshot = None
        self._pending_entities.clear()
        self._command_correlations.clear()
        if self._unsubscribe_state is not None:
            self._unsubscribe_state()
            self._unsubscribe_state = None

    async def async_stop(self) -> None:
        """Idempotently stop state tracking and invalidate this Config Entry."""
        self.invalidate()

