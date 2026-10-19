"""Platform allowing several lights to be grouped into one with relative brightness control."""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime
import itertools
import logging
import time
from typing import Any, cast

from homeassistant.components import light
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_MODE,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_EFFECT_LIST,
    ATTR_FLASH,
    ATTR_HS_COLOR,
    ATTR_MAX_COLOR_TEMP_KELVIN,
    ATTR_MIN_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ATTR_RGBW_COLOR,
    ATTR_RGBWW_COLOR,
    ATTR_SUPPORTED_COLOR_MODES,
    ATTR_TRANSITION,
    ATTR_WHITE,
    ATTR_XY_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
    filter_supported_color_modes,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ASSUMED_STATE,
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    CONF_ENTITIES,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Context,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .brightness import (
    BRIGHTNESS_MAX,
    BRIGHTNESS_MIN,
    base_relative_brightness_map,
    group_entity_ids_by_brightness,
    relative_brightness_map,
    representative_brightness,
)
from .const import (
    CONF_ALL,
    CONF_BRIGHTNESS_STRATEGY,
    CONF_DEBOUNCE_ENABLED,
    CONF_DEBOUNCE_TIME,
    CONF_MEMBER_DIAGNOSTICS,
    CONF_REMEMBER_BRIGHTNESS,
    CONF_REMEMBER_ON_STATE,
    CONF_RESTORE_INDIVIDUAL_BRIGHTNESS,
    DEFAULT_BRIGHTNESS_STRATEGY,
    DEFAULT_DEBOUNCE_ENABLED,
    DEFAULT_DEBOUNCE_TIME,
    DEFAULT_MEMBER_DIAGNOSTICS,
)
from .entity import GroupEntity
from .util import (
    coerce_in,
    find_state_attributes,
    mean_circle,
    mean_tuple,
    reduce_attribute,
)

PARALLEL_UPDATES = 0

SUPPORT_GROUP_LIGHT = (
    LightEntityFeature.EFFECT
    | LightEntityFeature.FLASH
    | LightEntityFeature.TRANSITION
)

_LOGGER = logging.getLogger(__name__)

FORWARDED_ATTRIBUTES = frozenset(
    {
        ATTR_BRIGHTNESS,
        ATTR_COLOR_TEMP_KELVIN,
        ATTR_EFFECT,
        ATTR_FLASH,
        ATTR_HS_COLOR,
        ATTR_RGB_COLOR,
        ATTR_RGBW_COLOR,
        ATTR_RGBWW_COLOR,
        ATTR_TRANSITION,
        ATTR_WHITE,
        ATTR_XY_COLOR,
    }
)

# Attributes that represent visual changes (not brightness or on/off control)
VISUAL_ATTRIBUTES = frozenset(
    {
        ATTR_COLOR_TEMP_KELVIN,
        ATTR_EFFECT,
        ATTR_FLASH,
        ATTR_HS_COLOR,
        ATTR_RGB_COLOR,
        ATTR_RGBW_COLOR,
        ATTR_RGBWW_COLOR,
        ATTR_WHITE,
        ATTR_XY_COLOR,
    }
)

VALID_MEMBER_STATES = frozenset({STATE_ON, STATE_OFF})


@dataclass(slots=True)
class CommandReadiness:
    """Execution mode resolved for a group command."""

    optimistic_mode: bool
    debounce_mode: bool
    optimistic_brightness: int | None = None


@dataclass(slots=True)
class MemberStateSnapshot:
    """Resolved view of the group's members for state-based decisions."""

    enabled_entity_ids: list[str]
    disabled_entity_ids: list[str]
    missing_entity_ids: list[str]
    unavailable_entity_ids: list[str]
    unknown_entity_ids: list[str]
    off_entity_ids: list[str]
    on_entity_ids: list[str]
    enabled_states: list[State]
    valid_states: list[State]
    on_states: list[State]
    valid_states_by_id: dict[str, State]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Initialize Relative Light Group config entry."""
    registry = er.async_get(hass)
    entities = er.async_validate_entity_ids(
        registry, config_entry.options[CONF_ENTITIES]
    )
    mode = config_entry.options.get(CONF_ALL, False)
    remember_on_state = config_entry.options.get(CONF_REMEMBER_ON_STATE, False)
    restore_individual_brightness = config_entry.options.get(
        CONF_RESTORE_INDIVIDUAL_BRIGHTNESS, False
    )
    remember_brightness = config_entry.options.get(CONF_REMEMBER_BRIGHTNESS, False)
    debounce_enabled = config_entry.options.get(
        CONF_DEBOUNCE_ENABLED, DEFAULT_DEBOUNCE_ENABLED
    )
    debounce_time = config_entry.options.get(CONF_DEBOUNCE_TIME, DEFAULT_DEBOUNCE_TIME)
    brightness_strategy = config_entry.options.get(
        CONF_BRIGHTNESS_STRATEGY, DEFAULT_BRIGHTNESS_STRATEGY
    )
    member_diagnostics = config_entry.options.get(
        CONF_MEMBER_DIAGNOSTICS, DEFAULT_MEMBER_DIAGNOSTICS
    )

    async_add_entities(
        [
            RelativeLightGroup(
                config_entry.entry_id,
                config_entry.title,
                entities,
                mode,
                remember_on_state,
                restore_individual_brightness,
                remember_brightness,
                debounce_enabled,
                debounce_time,
                brightness_strategy,
                member_diagnostics,
            )
        ]
    )


@callback
def async_create_preview_light(
    hass: HomeAssistant, name: str, validated_config: dict[str, Any]
) -> RelativeLightGroup:
    """Create a preview light entity."""
    return RelativeLightGroup(
        None,
        name,
        validated_config[CONF_ENTITIES],
        validated_config.get(CONF_ALL, False),
        validated_config.get(CONF_REMEMBER_ON_STATE, False),
        validated_config.get(CONF_RESTORE_INDIVIDUAL_BRIGHTNESS, False),
        validated_config.get(CONF_REMEMBER_BRIGHTNESS, False),
        validated_config.get(CONF_DEBOUNCE_ENABLED, DEFAULT_DEBOUNCE_ENABLED),
        validated_config.get(CONF_DEBOUNCE_TIME, DEFAULT_DEBOUNCE_TIME),
        validated_config.get(CONF_BRIGHTNESS_STRATEGY, DEFAULT_BRIGHTNESS_STRATEGY),
        validated_config.get(CONF_MEMBER_DIAGNOSTICS, DEFAULT_MEMBER_DIAGNOSTICS),
    )


class RelativeLightGroup(GroupEntity, LightEntity):
    """Representation of a relative light group.

    Brightness changes are distributed proportionally among on lights.
    Color/effect changes are forwarded only to on lights.
    Turn on/off affects all lights (with optional remember behavior).
    When remember_brightness is enabled, base brightness ratios are preserved
    even after hitting brightness limits (0% or 100%).
    When restore_individual_brightness is enabled, turning the group off stores
    each on member's brightness; turning the group back on reapplies it.
    """

    _attr_available = False
    _attr_translation_key = "relative_light_group"
    _attr_max_color_temp_kelvin = 6500
    _attr_min_color_temp_kelvin = 2000
    _attr_should_poll = False

    def __init__(
        self,
        unique_id: str | None,
        name: str,
        entity_ids: list[str],
        mode: bool | None,
        remember_on_state: bool,
        restore_individual_brightness: bool,
        remember_brightness: bool,
        debounce_enabled: bool,
        debounce_time: int,
        brightness_strategy: str,
        member_diagnostics: bool,
    ) -> None:
        """Initialize a relative light group."""
        self._entity_ids = entity_ids
        self._attr_name = name
        self._attr_extra_state_attributes = {ATTR_ENTITY_ID: entity_ids}
        self._attr_unique_id = unique_id
        self.mode = any
        if mode:
            self.mode = all

        self._remember_on_state = remember_on_state
        self._remembered_lights: list[str] | None = None
        self._remembered_lights_consistent = False

        self._restore_individual_brightness = restore_individual_brightness
        self._remembered_brightness: dict[str, int] = {}
        self._remembered_brightness_consistent = False

        self._remember_brightness = remember_brightness
        self._base_brightness: dict[str, int] = {}
        self._last_command_contexts: deque[str] = deque(maxlen=50)

        self._debounce_enabled = debounce_enabled
        self._debounce_time = debounce_time
        self._last_command_time = 0.0
        self._last_command_debounce_eligible = False
        self._debounce_sync_unsub: CALLBACK_TYPE | None = None
        self._brightness_strategy = brightness_strategy
        self._member_diagnostics = member_diagnostics

        self._attr_color_mode = ColorMode.UNKNOWN
        self._attr_supported_color_modes = {ColorMode.ONOFF}

    async def async_added_to_hass(self) -> None:
        """Register listeners and cancel debounce sync on remove."""
        await super().async_added_to_hass()

        def _cancel_debounce_sync() -> None:
            self._async_cancel_debounce_sync()

        self.async_on_remove(_cancel_debounce_sync)

    @callback
    def _async_cancel_debounce_sync(self) -> None:
        """Cancel a pending post-debounce member sync."""
        if self._debounce_sync_unsub is not None:
            self._debounce_sync_unsub()
            self._debounce_sync_unsub = None

    @callback
    def _update_assumed_state_from_members(self) -> None:
        """Update assumed_state based only on enabled members."""
        self._attr_assumed_state = False
        for entity_id in self._get_enabled_entity_ids():
            if (state := self.hass.states.get(entity_id)) is None:
                continue
            if state.attributes.get(ATTR_ASSUMED_STATE):
                self._attr_assumed_state = True
                return

    @callback
    def _is_member_enabled(self, entity_id: str) -> bool:
        """Return True when the member is not disabled in the entity registry."""
        registry_entry = er.async_get(self.hass).async_get(entity_id)
        return registry_entry is None or registry_entry.disabled_by is None

    @callback
    def _get_enabled_entity_ids(self) -> list[str]:
        """Return configured member IDs excluding disabled entities."""
        return [
            entity_id
            for entity_id in self._entity_ids
            if self._is_member_enabled(entity_id)
        ]

    @callback
    def _get_member_snapshot(self) -> MemberStateSnapshot:
        """Collect enabled members and classify which ones have a valid light state."""
        registry = er.async_get(self.hass)
        enabled_entity_ids: list[str] = []
        disabled_entity_ids: list[str] = []
        missing_entity_ids: list[str] = []
        unavailable_entity_ids: list[str] = []
        unknown_entity_ids: list[str] = []
        off_entity_ids: list[str] = []
        on_entity_ids: list[str] = []
        enabled_states: list[State] = []
        valid_states: list[State] = []
        on_states: list[State] = []
        valid_states_by_id: dict[str, State] = {}

        for entity_id in self._entity_ids:
            registry_entry = registry.async_get(entity_id)
            state = self.hass.states.get(entity_id)

            if registry_entry is not None and registry_entry.disabled_by is not None:
                disabled_entity_ids.append(entity_id)
                continue

            enabled_entity_ids.append(entity_id)

            if state is None:
                if registry_entry is None:
                    missing_entity_ids.append(entity_id)
                continue

            enabled_states.append(state)

            if state.state == STATE_UNAVAILABLE:
                unavailable_entity_ids.append(entity_id)
            elif state.state == STATE_UNKNOWN:
                unknown_entity_ids.append(entity_id)
            elif state.state == STATE_OFF:
                off_entity_ids.append(entity_id)
            elif state.state == STATE_ON:
                on_entity_ids.append(entity_id)

            if state.state not in VALID_MEMBER_STATES:
                continue

            valid_states.append(state)
            valid_states_by_id[state.entity_id] = state
            if state.state == STATE_ON:
                on_states.append(state)

        return MemberStateSnapshot(
            enabled_entity_ids=enabled_entity_ids,
            disabled_entity_ids=disabled_entity_ids,
            missing_entity_ids=missing_entity_ids,
            unavailable_entity_ids=unavailable_entity_ids,
            unknown_entity_ids=unknown_entity_ids,
            off_entity_ids=off_entity_ids,
            on_entity_ids=on_entity_ids,
            enabled_states=enabled_states,
            valid_states=valid_states,
            on_states=on_states,
            valid_states_by_id=valid_states_by_id,
        )

    def _get_valid_remembered_lights(self) -> list[str] | None:
        """Return remembered lights only when they match current membership."""
        if self._remembered_lights is None:
            return None

        allowed = set(self._get_enabled_entity_ids())
        seen: set[str] = set()
        remembered: list[str] = []

        for entity_id in self._remembered_lights:
            if entity_id not in allowed or entity_id in seen:
                return None
            seen.add(entity_id)
            remembered.append(entity_id)

        return remembered

    def _resolve_turn_on_target_entity_ids(self) -> list[str]:
        """Resolve the actual targets used for a group turn_on."""
        remembered = self._get_valid_remembered_lights()
        if self._remember_on_state and self._remembered_lights_consistent and remembered:
            return remembered
        return self._get_enabled_entity_ids()

    def _has_consistent_turn_on_targets(self) -> bool:
        """Return True when turn_on targets are known without fallback guesses."""
        if not self._remember_on_state:
            return True

        if not self._remembered_lights_consistent:
            return False

        remembered = self._get_valid_remembered_lights()
        return bool(remembered)

    def _state_supports_brightness(self, state: Any) -> bool | None:
        """Infer whether a member should contribute to group brightness."""
        if state is None or state.state not in VALID_MEMBER_STATES:
            return None

        if state.attributes.get(ATTR_BRIGHTNESS) is not None:
            return True

        supported_modes = state.attributes.get(ATTR_SUPPORTED_COLOR_MODES)
        if supported_modes is not None:
            return any(mode != ColorMode.ONOFF for mode in supported_modes)

        color_mode = state.attributes.get(ATTR_COLOR_MODE)
        if color_mode is not None:
            return color_mode != ColorMode.ONOFF

        return None

    def _get_restore_brightness_readiness(
        self, target_entity_ids: list[str], states_by_id: dict[str, Any]
    ) -> tuple[bool, int | None]:
        """Validate whether restored brightness supports optimistic turn_on."""
        if not self._remembered_brightness_consistent:
            return False, None

        brightness_values: list[int] = []
        brightness_required = False

        for entity_id in target_entity_ids:
            supports_brightness = self._state_supports_brightness(
                states_by_id.get(entity_id)
            )
            if supports_brightness is None:
                continue
            if not supports_brightness:
                continue

            brightness_required = True
            stored = self._remembered_brightness.get(entity_id)
            if stored is None:
                return False, None

            brightness_values.append(
                coerce_in(int(stored), BRIGHTNESS_MIN, BRIGHTNESS_MAX)
            )

        if not brightness_required:
            return True, None
        if not brightness_values:
            return False, None

        return True, int(sum(brightness_values) / len(brightness_values))

    def _seed_runtime_state_from_members(self, snapshot: MemberStateSnapshot) -> None:
        """Derive runtime-only snapshots from real member state when possible."""
        if not snapshot.valid_states or not snapshot.on_states:
            return

        on_entity_ids = [state.entity_id for state in snapshot.on_states]

        if self._remember_on_state and not self._has_consistent_turn_on_targets():
            self._remembered_lights = on_entity_ids
            self._remembered_lights_consistent = True

        if self._restore_individual_brightness:
            restore_ready, _ = self._get_restore_brightness_readiness(
                on_entity_ids, snapshot.valid_states_by_id
            )
            if not restore_ready:
                derived_brightness = {
                    state.entity_id: int(brightness)
                    for state in snapshot.on_states
                    if (brightness := state.attributes.get(ATTR_BRIGHTNESS)) is not None
                }
                if derived_brightness:
                    self._remembered_brightness = derived_brightness
                    self._remembered_brightness_consistent = True

    def _assess_turn_on_readiness(
        self, data: dict[str, Any], was_off: bool
    ) -> CommandReadiness:
        """Decide whether turn_on can safely use optimistic state and debounce."""
        snapshot = self._get_member_snapshot()
        self._seed_runtime_state_from_members(snapshot)

        if not snapshot.valid_states:
            return CommandReadiness(False, False)

        if was_off and not self._has_consistent_turn_on_targets():
            return CommandReadiness(False, False)

        if ATTR_BRIGHTNESS in data:
            return CommandReadiness(True, self._debounce_enabled, data[ATTR_BRIGHTNESS])

        if was_off:
            target_entity_ids = self._resolve_turn_on_target_entity_ids()
            states_by_id = snapshot.valid_states_by_id

            if self._restore_individual_brightness:
                restore_ready, optimistic_brightness = (
                    self._get_restore_brightness_readiness(
                        target_entity_ids, states_by_id
                    )
                )
                if not restore_ready:
                    return CommandReadiness(False, False)
                return CommandReadiness(
                    True,
                    self._debounce_enabled,
                    optimistic_brightness,
                )

            has_known_brightness_targets = False
            for entity_id in target_entity_ids:
                supports_brightness = self._state_supports_brightness(
                    states_by_id.get(entity_id)
                )
                if supports_brightness is None:
                    continue
                if supports_brightness:
                    has_known_brightness_targets = True

            if has_known_brightness_targets:
                return CommandReadiness(False, False)

        return CommandReadiness(True, self._debounce_enabled)

    def _assess_turn_off_readiness(self) -> CommandReadiness:
        """Decide whether turn_off can safely use optimistic state and debounce."""
        if not self._get_member_snapshot().valid_states:
            return CommandReadiness(False, False)
        return CommandReadiness(True, self._debounce_enabled)

    def _assess_command_readiness(
        self, command: str, data: dict[str, Any], *, was_off: bool = False
    ) -> CommandReadiness:
        """Resolve the execution mode for a command."""
        if command == SERVICE_TURN_ON:
            return self._assess_turn_on_readiness(data, was_off)
        if command == SERVICE_TURN_OFF:
            return self._assess_turn_off_readiness()
        return CommandReadiness(False, False)

    def _prepare_command_execution(self, readiness: CommandReadiness) -> None:
        """Prime debounce bookkeeping for the command about to run."""
        self._async_cancel_debounce_sync()
        self._last_command_debounce_eligible = readiness.debounce_mode
        self._last_command_time = time.monotonic()
        _LOGGER.debug(
            "Command readiness resolved: optimistic=%s debounce=%s brightness=%s",
            readiness.optimistic_mode,
            readiness.debounce_mode,
            readiness.optimistic_brightness,
        )

        if self._context and self._context.id not in self._last_command_contexts:
            self._last_command_contexts.append(self._context.id)

    @callback
    def _async_sync_group_state(self, *, ignore_debounce: bool = False) -> None:
        """Refresh derived state and write it if the update succeeded."""
        if not self.hass.is_running:
            return
        if self.async_update_group_state(ignore_debounce=ignore_debounce):
            self.async_write_ha_state()

    @callback
    def _async_write_optimistic_state(self) -> None:
        """Publish the optimistic group state immediately."""
        if self._member_diagnostics:
            self._attr_extra_state_attributes = self._build_extra_state_attributes(
                self._get_member_snapshot()
            )
        if self.hass.is_running:
            self.async_write_ha_state()

    @callback
    def _async_schedule_post_command_sync(self) -> None:
        """Sync once after the current command has had time to settle."""
        if self._debounce_sync_unsub is not None:
            return

        if not self._debounce_enabled or self._debounce_time <= 0:
            self._async_sync_group_state(ignore_debounce=True)
            return

        remaining = self._debounce_time / 1000 - (
            time.monotonic() - self._last_command_time
        )
        if remaining <= 0:
            self._async_sync_group_state(ignore_debounce=True)
            return

        @callback
        def _sync(_dt: datetime) -> None:
            self._debounce_sync_unsub = None
            self._async_sync_group_state(ignore_debounce=True)

        self._debounce_sync_unsub = async_call_later(self.hass, remaining, _sync)

    def _context_belongs_to_group_command(self, context: Context | None) -> bool:
        """Return True if the context was produced by a recent group command."""
        if context is None:
            return False
        return (
            context.id in self._last_command_contexts
            or context.parent_id in self._last_command_contexts
        )

    def _event_context_belongs_to_group_command(
        self, event: Event[EventStateChangedData]
    ) -> bool:
        """Return True if the member event came from this group."""
        new_state = event.data.get("new_state")
        if new_state is not None and new_state.context is not None:
            return self._context_belongs_to_group_command(new_state.context)
        return self._context_belongs_to_group_command(event.context)

    @callback
    def async_should_defer_state_change(
        self,
        event: Event[EventStateChangedData],
    ) -> bool:
        """Ignore only group-originated member updates during debounce."""
        if not self._last_command_debounce_eligible:
            return False

        if not self._debounce_enabled or self._debounce_time <= 0:
            return False

        if time.monotonic() - self._last_command_time >= self._debounce_time / 1000:
            return False

        if not self._event_context_belongs_to_group_command(event):
            return False

        self._async_schedule_post_command_sync()
        return True

    @callback
    def _is_debounce_active(self) -> bool:
        """Return True while a group-originated debounce window is active."""
        return (
            self._last_command_debounce_eligible
            and self._debounce_enabled
            and self._debounce_time > 0
            and time.monotonic() - self._last_command_time < self._debounce_time / 1000
        )

    @callback
    def _build_extra_state_attributes(
        self, snapshot: MemberStateSnapshot
    ) -> dict[str, Any]:
        """Build group attributes, adding member diagnostics only when requested."""
        attributes: dict[str, Any] = {ATTR_ENTITY_ID: self._entity_ids}
        if not self._member_diagnostics:
            return attributes

        member_states: dict[str, str] = {}
        state_by_id = {state.entity_id: state for state in snapshot.enabled_states}
        for entity_id in self._entity_ids:
            if entity_id in snapshot.disabled_entity_ids:
                member_states[entity_id] = "disabled"
            elif entity_id in snapshot.missing_entity_ids:
                member_states[entity_id] = "missing"
            elif (state := state_by_id.get(entity_id)) is not None:
                member_states[entity_id] = state.state
            else:
                member_states[entity_id] = "missing"

        member_brightness = {
            state.entity_id: int(brightness)
            for state in snapshot.enabled_states
            if (brightness := state.attributes.get(ATTR_BRIGHTNESS)) is not None
        }

        attributes.update(
            {
                "member_states": member_states,
                "member_brightness": member_brightness,
                "remembered_on_members": self._remembered_lights or [],
                "remembered_on_members_consistent": self._remembered_lights_consistent,
                "remembered_brightness": dict(self._remembered_brightness),
                "remembered_brightness_consistent": (
                    self._remembered_brightness_consistent
                ),
                CONF_BRIGHTNESS_STRATEGY: self._brightness_strategy,
                "debounce_active": self._is_debounce_active(),
            }
        )
        return attributes

    def _get_on_lights(self) -> list:
        """Get list of currently on light states."""
        return self._get_member_snapshot().on_states

    def _ensure_base_brightness(self, on_lights: list) -> None:
        """Ensure base brightness is captured for all on lights."""
        for state in on_lights:
            eid = state.entity_id
            if eid not in self._base_brightness:
                brightness = state.attributes.get(ATTR_BRIGHTNESS)
                if brightness is not None:
                    self._base_brightness[eid] = int(brightness)

    async def _async_call_light_service(
        self,
        service: str,
        call_data: dict[str, Any],
    ) -> HomeAssistantError | None:
        """Call a light service and return the service error, if any."""
        try:
            await self.hass.services.async_call(
                light.DOMAIN,
                service,
                call_data,
                blocking=True,
                context=self._context,
            )
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Light service %s failed for %s: %s",
                service,
                call_data.get(ATTR_ENTITY_ID),
                err,
            )
            return err
        return None

    @staticmethod
    def _raise_if_all_light_service_calls_failed(
        errors: list[HomeAssistantError], success_count: int
    ) -> None:
        """Raise the first service error only when no grouped call succeeded."""
        if errors and success_count == 0:
            raise errors[0]

    async def _async_call_light_service_batches(
        self, call_data_batches: list[dict[str, Any]]
    ) -> None:
        """Call independent light service batches concurrently."""
        results = await asyncio.gather(
            *(
                self._async_call_light_service(SERVICE_TURN_ON, call_data)
                for call_data in call_data_batches
            )
        )
        errors = [result for result in results if result is not None]
        self._raise_if_all_light_service_calls_failed(
            errors, len(results) - len(errors)
        )

    async def _apply_brightness_with_base(
        self,
        data: dict[str, Any],
        on_lights: list,
        target_brightness: int,
    ) -> None:
        """Apply brightness using base-relative algorithm.

        When going UP from base: uses relative headroom distribution
        (all lights reach max together). When going DOWN: scales
        proportionally from base (preserving ratios perfectly).
        Always references base brightness, so ratios are never lost
        even after hitting limits.
        """
        self._ensure_base_brightness(on_lights)
        brightness_map = base_relative_brightness_map(
            on_lights, self._base_brightness, target_brightness
        )

        if not brightness_map:
            return

        visual_data = {
            key: value for key, value in data.items() if key in VISUAL_ATTRIBUTES
        }

        call_data_batches: list[dict[str, Any]] = []
        for br, eids in group_entity_ids_by_brightness(brightness_map).items():
            call_data = {**visual_data}
            call_data[ATTR_BRIGHTNESS] = br
            call_data[ATTR_ENTITY_ID] = eids
            if ATTR_TRANSITION in data:
                call_data[ATTR_TRANSITION] = data[ATTR_TRANSITION]

            _LOGGER.debug("Base-relative brightness call: %s", call_data)
            call_data_batches.append(call_data)

        await self._async_call_light_service_batches(call_data_batches)

    async def _async_turn_on_targets_from_group_off(
        self, data: dict[str, Any], target_entity_ids: list[str]
    ) -> None:
        """Turn on targets after the group was off; optionally restore saved brightness per member."""
        if not target_entity_ids:
            return

        use_restore = (
            self._restore_individual_brightness
            and self._remembered_brightness_consistent
            and ATTR_BRIGHTNESS not in data
            and bool(self._remembered_brightness)
        )
        visual_data = {
            key: value
            for key, value in data.items()
            if key in VISUAL_ATTRIBUTES or key == ATTR_TRANSITION
        }
        if use_restore:
            by_brightness: dict[int, list[str]] = {}
            no_stored_brightness: list[str] = []
            for eid in target_entity_ids:
                stored = self._remembered_brightness.get(eid)
                if stored is not None:
                    bri = coerce_in(int(stored), BRIGHTNESS_MIN, BRIGHTNESS_MAX)
                    by_brightness.setdefault(bri, []).append(eid)
                else:
                    no_stored_brightness.append(eid)
            call_data_batches: list[dict[str, Any]] = []
            for brightness, eids in by_brightness.items():
                call_data = {
                    **visual_data,
                    ATTR_BRIGHTNESS: brightness,
                    ATTR_ENTITY_ID: eids,
                }
                if ATTR_TRANSITION in data:
                    call_data[ATTR_TRANSITION] = data[ATTR_TRANSITION]
                _LOGGER.debug("Restore brightness turn_on: %s", call_data)
                call_data_batches.append(call_data)
            if no_stored_brightness:
                call_data = {**visual_data, ATTR_ENTITY_ID: no_stored_brightness}
                if ATTR_TRANSITION in data:
                    call_data[ATTR_TRANSITION] = data[ATTR_TRANSITION]
                _LOGGER.debug("Turn on without stored brightness: %s", call_data)
                call_data_batches.append(call_data)
            await self._async_call_light_service_batches(call_data_batches)
            return

        data[ATTR_ENTITY_ID] = target_entity_ids
        _LOGGER.debug("Turning on group (was off): %s", data)

        if err := await self._async_call_light_service(SERVICE_TURN_ON, data):
            raise err

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Forward the turn_on command with relative brightness control.

        Behavior:
        - If the group is OFF (being turned on):
          - With remember_on_state: restore only previously-on lights
          - Without remember_on_state: turn on all lights
          - With restore_individual_brightness (and no explicit group brightness):
            reapply each target's saved brightness from before the last group off
        - If brightness is changing:
          - With remember_brightness: use base-relative algorithm
          - Without remember_brightness: use standard relative algorithm
        - If color/effect is changing: only apply to on lights
        """
        data = {
            key: value for key, value in kwargs.items() if key in FORWARDED_ATTRIBUTES
        }

        was_off = not self._attr_is_on
        readiness = self._assess_command_readiness(
            SERVICE_TURN_ON, data, was_off=was_off
        )
        self._prepare_command_execution(readiness)

        if readiness.optimistic_mode:
            self._attr_is_on = True
            if readiness.optimistic_brightness is not None:
                self._attr_brightness = readiness.optimistic_brightness
            if ATTR_HS_COLOR in data:
                self._attr_hs_color = data[ATTR_HS_COLOR]
            if ATTR_RGB_COLOR in data:
                self._attr_rgb_color = data[ATTR_RGB_COLOR]
            if ATTR_COLOR_TEMP_KELVIN in data:
                self._attr_color_temp_kelvin = data[ATTR_COLOR_TEMP_KELVIN]
            self._async_write_optimistic_state()

        on_lights = self._get_on_lights()
        has_brightness = ATTR_BRIGHTNESS in data
        has_visual_attrs = any(key in data for key in VISUAL_ATTRIBUTES)

        try:
            # Case 1: Group was OFF → turning on
            if was_off:
                target_entity_ids = self._resolve_turn_on_target_entity_ids()

                await self._async_turn_on_targets_from_group_off(data, target_entity_ids)
                return

            # Case 2: Group is ON and brightness is being changed
            if has_brightness and on_lights:
                if self._remember_brightness:
                    # Use base-relative algorithm (preserves ratios)
                    await self._apply_brightness_with_base(
                        data, on_lights, data[ATTR_BRIGHTNESS]
                    )

                else:
                    # Standard relative algorithm
                    await self._apply_relative_brightness(data, on_lights)
                return

            # Case 3: Group is ON, no brightness change, but visual attributes
            if has_visual_attrs and on_lights:
                on_entity_ids = [state.entity_id for state in on_lights]
                visual_data = {
                    key: value
                    for key, value in data.items()
                    if key in VISUAL_ATTRIBUTES or key == ATTR_TRANSITION
                }
                visual_data[ATTR_ENTITY_ID] = on_entity_ids

                _LOGGER.debug("Visual-only change to on lights: %s", visual_data)

                if err := await self._async_call_light_service(
                    SERVICE_TURN_ON, visual_data
                ):
                    raise err
                return

            # Case 4: Fallback – no special handling needed
            data[ATTR_ENTITY_ID] = self._get_enabled_entity_ids()
            if not data[ATTR_ENTITY_ID]:
                return

            _LOGGER.debug("Forwarded turn_on command: %s", data)

            if err := await self._async_call_light_service(SERVICE_TURN_ON, data):
                raise err
        finally:
            if readiness.debounce_mode:
                self._async_schedule_post_command_sync()
            else:
                self._async_sync_group_state(ignore_debounce=True)

    async def _apply_relative_brightness(
        self, data: dict[str, Any], on_lights: list
    ) -> None:
        """Apply standard relative brightness algorithm (oscarb-style)."""
        # The optimistic state already holds the target, so measure the members.
        group_brightness_current = representative_brightness(
            on_lights, ATTR_BRIGHTNESS, self._brightness_strategy
        )
        if group_brightness_current is None or group_brightness_current <= 0:
            return

        group_brightness_new = data[ATTR_BRIGHTNESS]
        brightness_map = relative_brightness_map(
            on_lights,
            group_brightness_current,
            group_brightness_new,
            ATTR_BRIGHTNESS,
        )

        visual_data = {
            key: value for key, value in data.items() if key in VISUAL_ATTRIBUTES
        }

        if brightness_map:
            call_data_batches: list[dict[str, Any]] = []
            for brightness, entity_ids in group_entity_ids_by_brightness(
                brightness_map
            ).items():
                call_data = {**visual_data}
                call_data[ATTR_BRIGHTNESS] = brightness
                call_data[ATTR_ENTITY_ID] = entity_ids
                if ATTR_TRANSITION in data:
                    call_data[ATTR_TRANSITION] = data[ATTR_TRANSITION]

                _LOGGER.debug("Relative brightness call: %s", call_data)
                call_data_batches.append(call_data)

            await self._async_call_light_service_batches(call_data_batches)
        elif visual_data:
            on_entity_ids = [state.entity_id for state in on_lights]
            call_data = {**visual_data}
            call_data[ATTR_ENTITY_ID] = on_entity_ids
            if ATTR_TRANSITION in data:
                call_data[ATTR_TRANSITION] = data[ATTR_TRANSITION]

            if err := await self._async_call_light_service(SERVICE_TURN_ON, call_data):
                raise err

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Forward the turn_off command to all lights in the light group."""
        readiness = self._assess_command_readiness(SERVICE_TURN_OFF, {})
        self._prepare_command_execution(readiness)
        if readiness.optimistic_mode:
            self._attr_is_on = False
            self._async_write_optimistic_state()

        snapshot = self._get_member_snapshot()
        state_snapshot_consistent = bool(snapshot.valid_states)

        # Remember which lights are on before turning off
        if self._remember_on_state:
            self._remembered_lights = [state.entity_id for state in snapshot.on_states]
            self._remembered_lights_consistent = state_snapshot_consistent
            _LOGGER.debug("Remembered on lights: %s", self._remembered_lights)

        # Snapshot brightness from current HA state before members change (avoids races).
        if self._restore_individual_brightness:
            self._remembered_brightness = {}
            for state in snapshot.on_states:
                bri = state.attributes.get(ATTR_BRIGHTNESS)
                if bri is not None:
                    self._remembered_brightness[state.entity_id] = int(bri)
            self._remembered_brightness_consistent = state_snapshot_consistent
            _LOGGER.debug("Remembered brightness: %s", self._remembered_brightness)
        else:
            self._remembered_brightness.clear()
            self._remembered_brightness_consistent = False

        data = {ATTR_ENTITY_ID: snapshot.enabled_entity_ids}

        if ATTR_TRANSITION in kwargs:
            data[ATTR_TRANSITION] = kwargs[ATTR_TRANSITION]

        try:
            if not data[ATTR_ENTITY_ID]:
                return
            if err := await self._async_call_light_service(SERVICE_TURN_OFF, data):
                raise err
        finally:
            if readiness.debounce_mode:
                self._async_schedule_post_command_sync()
            else:
                self._async_sync_group_state(ignore_debounce=True)

    @callback
    def async_update_group_state(self, *, ignore_debounce: bool = False) -> bool:
        """Query all members and determine the light group state."""
        self._update_assumed_state_from_members()
        snapshot = self._get_member_snapshot()
        valid_states = snapshot.valid_states
        on_states = snapshot.on_states

        if valid_states:
            self._attr_is_on = self.mode(
                state.state == STATE_ON for state in valid_states
            )
            self._attr_available = True
        else:
            self._attr_is_on = None
            self._attr_available = bool(snapshot.enabled_entity_ids) and any(
                state.state != STATE_UNAVAILABLE for state in snapshot.enabled_states
            )

        # Brightness is calculated only from ON lights using the configured strategy.
        self._attr_brightness = representative_brightness(
            on_states, ATTR_BRIGHTNESS, self._brightness_strategy
        )
        self._seed_runtime_state_from_members(snapshot)

        # Update base brightness from external changes only.
        # Check each light's state context to see if it was driven by a group command.
        if self._remember_brightness:
            for state in on_states:
                # Only external changes should redefine the remembered brightness base.
                if not self._context_belongs_to_group_command(state.context):
                    brightness = state.attributes.get(ATTR_BRIGHTNESS)
                    if brightness is not None:
                        self._base_brightness[state.entity_id] = int(brightness)
                        _LOGGER.debug(
                            "Updated external base brightness for %s: %s",
                            state.entity_id,
                            brightness,
                        )

        self._attr_hs_color = reduce_attribute(
            on_states, ATTR_HS_COLOR, reduce=mean_circle
        )
        self._attr_rgb_color = reduce_attribute(
            on_states, ATTR_RGB_COLOR, reduce=mean_tuple
        )
        self._attr_rgbw_color = reduce_attribute(
            on_states, ATTR_RGBW_COLOR, reduce=mean_tuple
        )
        self._attr_rgbww_color = reduce_attribute(
            on_states, ATTR_RGBWW_COLOR, reduce=mean_tuple
        )
        self._attr_xy_color = reduce_attribute(
            on_states, ATTR_XY_COLOR, reduce=mean_tuple
        )

        self._attr_color_temp_kelvin = reduce_attribute(
            on_states, ATTR_COLOR_TEMP_KELVIN
        )
        self._attr_min_color_temp_kelvin = reduce_attribute(
            valid_states, ATTR_MIN_COLOR_TEMP_KELVIN, default=2000, reduce=min
        )
        self._attr_max_color_temp_kelvin = reduce_attribute(
            valid_states, ATTR_MAX_COLOR_TEMP_KELVIN, default=6500, reduce=max
        )

        self._attr_effect_list = None
        all_effect_lists = list(find_state_attributes(valid_states, ATTR_EFFECT_LIST))
        if all_effect_lists:
            self._attr_effect_list = list(set().union(*all_effect_lists))
            self._attr_effect_list.sort()
            if "None" in self._attr_effect_list:
                self._attr_effect_list.remove("None")
                self._attr_effect_list.insert(0, "None")

        self._attr_effect = None
        all_effects = list(find_state_attributes(on_states, ATTR_EFFECT))
        if all_effects:
            effects_count = Counter(itertools.chain(all_effects))
            self._attr_effect = effects_count.most_common(1)[0][0]

        supported_color_modes = {ColorMode.ONOFF}
        all_supported_color_modes = list(
            find_state_attributes(valid_states, ATTR_SUPPORTED_COLOR_MODES)
        )
        if all_supported_color_modes:
            supported_color_modes = filter_supported_color_modes(
                cast(set[ColorMode], set().union(*all_supported_color_modes))
            )
        self._attr_supported_color_modes = supported_color_modes

        self._attr_color_mode = ColorMode.UNKNOWN
        all_color_modes = list(find_state_attributes(on_states, ATTR_COLOR_MODE))
        if all_color_modes:
            color_mode_count = Counter(itertools.chain(all_color_modes))
            if ColorMode.ONOFF in color_mode_count:
                if ColorMode.ONOFF in supported_color_modes:
                    color_mode_count[ColorMode.ONOFF] = -1
                else:
                    color_mode_count.pop(ColorMode.ONOFF)
            if ColorMode.BRIGHTNESS in color_mode_count:
                if ColorMode.BRIGHTNESS in supported_color_modes:
                    color_mode_count[ColorMode.BRIGHTNESS] = 0
                else:
                    color_mode_count.pop(ColorMode.BRIGHTNESS)
            if color_mode_count:
                self._attr_color_mode = color_mode_count.most_common(1)[0][0]
            else:
                self._attr_color_mode = next(iter(supported_color_modes))

        self._attr_supported_features = LightEntityFeature(0)
        for support in find_state_attributes(valid_states, ATTR_SUPPORTED_FEATURES):
            self._attr_supported_features |= support
        self._attr_supported_features &= SUPPORT_GROUP_LIGHT

        self._attr_extra_state_attributes = self._build_extra_state_attributes(
            snapshot
        )
        return True
