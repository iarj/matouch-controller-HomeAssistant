"""Device-authoritative functional-page administration for MaTouch Options Flow."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

import voluptuous as vol

from homeassistant.helpers import selector

from .active_mask import (
    ACTIVE_MASK_OK,
    ActiveMaskObservation,
    ActiveMaskState,
    ActiveMaskTransportResponseError,
    ActiveMaskTransportUnavailableError,
    async_fetch_active_mask,
    async_set_active_mask,
)
from .config_flow import MatouchControllerOptionsFlow
from .const import CONF_MANIFEST_ACTION, CONF_PAGE_SLOT, DOMAIN
from .mapping import effective_entry_data
from .page_order import (
    PAGE_ORDER_OK,
    PageOrderObservation,
    PageOrderState,
    PageOrderTransportResponseError,
    PageOrderTransportUnavailableError,
    async_fetch_page_order,
    async_set_page_order,
    page_order_actions_available,
)

# Menu values double as selector translation keys (lowercase key format).
ACTIVE_MASK_SELECTION = "active_pages"
PAGE_ORDER_SELECTION = "page_order"
CONF_ACTIVE_PAGE_SLOTS = "active_page_slots"
CONF_PAGE_ORDER = "page_order"


def _compiled_mask(manifest) -> int:
    """Return the functional-page mask represented by one validated manifest."""
    mask = 0
    for page in manifest.pages:
        mask |= 1 << (page.page_slot - 1)
    return mask


def _compiled_order(manifest) -> tuple[int, ...]:
    """Return deterministic default order represented by one manifest."""
    return tuple(sorted(page.page_slot for page in manifest.pages))


def _mask_slots(mask: int) -> list[str]:
    """Return one-based slot strings selected by a functional mask."""
    return [str(slot) for slot in range(1, 7) if mask & (1 << (slot - 1))]


def _selected_mask(value: Any, allowed_slots: set[int]) -> int | None:
    """Validate one complete desired active-page selection and return its mask."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        return None

    selected: set[int] = set()
    for item in value:
        if isinstance(item, bool):
            return None
        if isinstance(item, int):
            slot = item
        elif isinstance(item, str) and item.isdigit():
            slot = int(item)
        else:
            return None
        if slot not in allowed_slots:
            return None
        selected.add(slot)

    if not selected:
        return None

    mask = 0
    for slot in selected:
        mask |= 1 << (slot - 1)
    return mask


def _selected_page_order(value: Any, allowed_slots: set[int]) -> tuple[int, ...] | None:
    """Parse one explicit full permutation from the page-order admin form."""
    values: Sequence[Any]
    if isinstance(value, str):
        values = tuple(part.strip() for part in value.split(",") if part.strip())
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        return None

    order: list[int] = []
    for item in values:
        if isinstance(item, bool):
            return None
        if isinstance(item, int):
            slot = item
        elif isinstance(item, str) and item.isdigit():
            slot = int(item)
        else:
            return None
        if slot not in allowed_slots or slot in order:
            return None
        order.append(slot)

    if len(order) != len(allowed_slots) or set(order) != allowed_slots:
        return None
    return tuple(order)


def build_active_pages_schema(manifest, active_mask: int) -> vol.Schema:
    """Build one multi-select for device-local functional-page activation."""
    options = [
        {
            "value": str(page.page_slot),
            "label": f"{page.page_slot}: {page.title}",
        }
        for page in manifest.pages
    ]
    return vol.Schema(
        {
            vol.Required(
                CONF_ACTIVE_PAGE_SLOTS,
                default=_mask_slots(active_mask),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            )
        }
    )


def build_page_order_schema(page_order: Sequence[int]) -> vol.Schema:
    """Build explicit comma-separated complete page order input."""
    return vol.Schema(
        {
            vol.Required(
                CONF_PAGE_ORDER,
                default=",".join(str(slot) for slot in page_order),
            ): str,
        }
    )


class MatouchControllerActiveMaskOptionsFlow(MatouchControllerOptionsFlow):
    """Extend page-first Options Flow with device-local page administration."""

    def __init__(self) -> None:
        super().__init__()
        self._active_mask_state: ActiveMaskState | None = None
        self._page_order_state: PageOrderState | None = None

    def _extra_menu_options(self) -> list[dict[str, str]]:
        """Add the supported device-local routes next to the mapping items."""
        options = [
            {
                "value": ACTIVE_MASK_SELECTION,
                "label": "Active pages · device",
            }
        ]

        manifest_action = effective_entry_data(self.config_entry).get(
            CONF_MANIFEST_ACTION
        )
        if page_order_actions_available(self.hass, manifest_action):
            options.append(
                {
                    "value": PAGE_ORDER_SELECTION,
                    "label": "Page order · device",
                }
            )
        return options

    async def async_step_page(
        self,
        user_input: dict[str, Any] | None = None,
    ):
        """Route device-local administration independently of mapping edits."""
        if user_input is not None:
            selection = user_input.get(CONF_PAGE_SLOT)
            if selection == ACTIVE_MASK_SELECTION:
                return await self.async_step_active_pages()
            if selection == PAGE_ORDER_SELECTION:
                return await self.async_step_page_order()
        return await super().async_step_page(user_input)

    def _runtime_active_masks(self) -> dict[str, ActiveMaskObservation] | None:
        data_store = getattr(self.hass, "data", None)
        if not isinstance(data_store, dict):
            return None
        runtime = data_store.get(DOMAIN)
        active_masks = getattr(runtime, "active_masks", None)
        return active_masks if isinstance(active_masks, dict) else None

    def _runtime_page_orders(self) -> dict[str, PageOrderObservation] | None:
        data_store = getattr(self.hass, "data", None)
        if not isinstance(data_store, dict):
            return None
        runtime = data_store.get(DOMAIN)
        page_orders = getattr(runtime, "page_orders", None)
        return page_orders if isinstance(page_orders, dict) else None

    def _device_uid(self) -> str | None:
        value = effective_entry_data(self.config_entry).get("device_uid")
        return value if isinstance(value, str) and value else None

    def _cache_observation(self, observation: ActiveMaskObservation) -> None:
        cache = self._runtime_active_masks()
        device_uid = self._device_uid()
        if cache is not None and device_uid is not None:
            cache[device_uid] = observation

    def _mark_observation_stale(self) -> None:
        cache = self._runtime_active_masks()
        device_uid = self._device_uid()
        if cache is None or device_uid is None:
            return
        previous = cache.get(device_uid, ActiveMaskObservation())
        cache[device_uid] = previous.marked_stale()

    def _cache_page_order_observation(self, observation: PageOrderObservation) -> None:
        cache = self._runtime_page_orders()
        device_uid = self._device_uid()
        if cache is not None and device_uid is not None:
            cache[device_uid] = observation

    def _mark_page_order_observation_stale(self) -> None:
        cache = self._runtime_page_orders()
        device_uid = self._device_uid()
        if cache is None or device_uid is None:
            return
        previous = cache.get(device_uid, PageOrderObservation())
        cache[device_uid] = previous.marked_stale()

    async def _async_read_active_mask(self) -> tuple[ActiveMaskState | None, str | None]:
        self._active_mask_state = None
        self._mark_observation_stale()
        manifest = self._runtime_manifest
        if manifest is None:
            return None, "active_mask_invalid_response"

        action = effective_entry_data(self.config_entry).get(CONF_MANIFEST_ACTION)
        if not isinstance(action, str) or not action:
            self._mark_observation_stale()
            return None, "active_mask_unavailable"

        try:
            state = await async_fetch_active_mask(self.hass, action)
        except ActiveMaskTransportUnavailableError:
            self._mark_observation_stale()
            return None, "active_mask_unavailable"
        except ActiveMaskTransportResponseError:
            self._mark_observation_stale()
            return None, "active_mask_invalid_response"

        if state.compiled_mask != _compiled_mask(manifest):
            self._mark_observation_stale()
            return None, "active_mask_invalid_response"

        self._active_mask_state = state
        self._cache_observation(ActiveMaskObservation.from_state(state))
        return state, None

    async def _async_read_page_order(self) -> tuple[PageOrderState | None, str | None]:
        self._page_order_state = None
        self._mark_page_order_observation_stale()
        manifest = self._runtime_manifest
        if manifest is None:
            return None, "page_order_invalid_response"

        action = effective_entry_data(self.config_entry).get(CONF_MANIFEST_ACTION)
        if not isinstance(action, str) or not action:
            return None, "page_order_unavailable"

        try:
            state = await async_fetch_page_order(self.hass, action)
        except PageOrderTransportUnavailableError:
            return None, "page_order_unavailable"
        except PageOrderTransportResponseError:
            return None, "page_order_invalid_response"

        if state.compiled_mask != _compiled_mask(manifest):
            return None, "page_order_invalid_response"

        self._page_order_state = state
        self._cache_page_order_observation(PageOrderObservation.from_state(state))
        return state, None

    def _active_pages_form(
        self,
        state: ActiveMaskState | None,
        *,
        error: str | None = None,
    ):
        manifest = self._runtime_manifest
        if manifest is None:
            return self.async_abort(reason="manifest_unavailable")
        default_mask = state.active_mask if state is not None else _compiled_mask(manifest)
        return self.async_show_form(
            step_id="active_pages",
            data_schema=build_active_pages_schema(manifest, default_mask),
            errors={} if error is None else {"base": error},
        )

    def _page_order_form(
        self,
        state: PageOrderState | None,
        *,
        error: str | None = None,
    ):
        manifest = self._runtime_manifest
        if manifest is None:
            return self.async_abort(reason="manifest_unavailable")
        default_order = state.page_order if state is not None else _compiled_order(manifest)
        return self.async_show_form(
            step_id="page_order",
            data_schema=build_page_order_schema(default_order),
            errors={} if error is None else {"base": error},
        )

    async def async_step_active_pages(
        self,
        user_input: dict[str, Any] | None = None,
    ):
        manifest = self._runtime_manifest
        if manifest is None:
            return self.async_abort(reason="manifest_unavailable")

        if user_input is None:
            state, error = await self._async_read_active_mask()
            if state is None:
                return self._active_pages_form(None, error=error)
            return self._active_pages_form(state)

        state = self._active_mask_state
        if state is None:
            state, error = await self._async_read_active_mask()
            if state is None:
                return self._active_pages_form(None, error=error)

        allowed_slots = {page.page_slot for page in manifest.pages}
        desired_mask = _selected_mask(
            user_input.get(CONF_ACTIVE_PAGE_SLOTS),
            allowed_slots,
        )
        if desired_mask is None:
            return self._active_pages_form(
                state,
                error="active_mask_invalid_selection",
            )

        action = effective_entry_data(self.config_entry).get(CONF_MANIFEST_ACTION)
        if not isinstance(action, str) or not action:
            self._active_mask_state = None
            self._mark_observation_stale()
            return self._active_pages_form(
                None,
                error="active_mask_result_unknown",
            )

        try:
            result = await async_set_active_mask(
                self.hass,
                action,
                desired_mask,
            )
        except (
            ActiveMaskTransportUnavailableError,
            ActiveMaskTransportResponseError,
        ):
            self._active_mask_state = None
            self._mark_observation_stale()
            return self._active_pages_form(
                None,
                error="active_mask_result_unknown",
            )

        if result.compiled_mask != _compiled_mask(manifest):
            self._active_mask_state = None
            self._mark_observation_stale()
            return self._active_pages_form(
                None,
                error="active_mask_invalid_response",
            )

        returned_state = ActiveMaskState(
            compiled_mask=result.compiled_mask,
            active_mask=result.active_mask,
        )
        self._active_mask_state = returned_state
        self._cache_observation(
            ActiveMaskObservation.from_state(
                returned_state,
                error_code=result.error_code,
            )
        )

        if result.error_code != ACTIVE_MASK_OK:
            return self._active_pages_form(
                returned_state,
                error="active_mask_rejected",
            )

        if result.active_mask != desired_mask:
            self._active_mask_state = None
            self._mark_observation_stale()
            return self._active_pages_form(
                None,
                error="active_mask_invalid_response",
            )

        return await super().async_step_page()

    async def _page_order_readback_after_ambiguity(self):
        self._page_order_state = None
        self._mark_page_order_observation_stale()
        state, _error = await self._async_read_page_order()
        return self._page_order_form(state, error="page_order_result_unknown")

    async def async_step_page_order(
        self,
        user_input: dict[str, Any] | None = None,
    ):
        manifest = self._runtime_manifest
        if manifest is None:
            return self.async_abort(reason="manifest_unavailable")

        if user_input is None:
            state, error = await self._async_read_page_order()
            if state is None:
                return self._page_order_form(None, error=error)
            return self._page_order_form(state)

        state = self._page_order_state
        if state is None:
            state, error = await self._async_read_page_order()
            if state is None:
                return self._page_order_form(None, error=error)

        allowed_slots = {page.page_slot for page in manifest.pages}
        desired_order = _selected_page_order(
            user_input.get(CONF_PAGE_ORDER),
            allowed_slots,
        )
        if desired_order is None:
            return self._page_order_form(
                state,
                error="page_order_invalid_selection",
            )

        action = effective_entry_data(self.config_entry).get(CONF_MANIFEST_ACTION)
        if not isinstance(action, str) or not action:
            self._page_order_state = None
            self._mark_page_order_observation_stale()
            return self._page_order_form(None, error="page_order_result_unknown")

        try:
            result = await async_set_page_order(
                self.hass,
                action,
                desired_order,
            )
        except (
            PageOrderTransportUnavailableError,
            PageOrderTransportResponseError,
        ):
            return await self._page_order_readback_after_ambiguity()

        if result.compiled_mask != _compiled_mask(manifest):
            return await self._page_order_readback_after_ambiguity()

        returned_state = PageOrderState(
            compiled_mask=result.compiled_mask,
            page_order=result.page_order,
        )
        self._page_order_state = returned_state
        self._cache_page_order_observation(
            PageOrderObservation.from_state(
                returned_state,
                error_code=result.error_code,
            )
        )

        if result.error_code != PAGE_ORDER_OK:
            return self._page_order_form(
                returned_state,
                error="page_order_rejected",
            )

        if result.page_order != desired_order:
            return await self._page_order_readback_after_ambiguity()

        return await super().async_step_page()
