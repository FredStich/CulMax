"""MaxCUL custom component for Home Assistant."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import voluptuous as vol

from homeassistant.components.http import StaticPathConfig
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICES, CONF_NAME, CONF_TYPE
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect, dispatcher_send
from homeassistant.helpers.typing import ConfigType

from ._vendor.pymaxcul import (
    EVENT_DEVICE_PAIRED,
    EVENT_DEVICE_REPAIRED,
    EVENT_FHT_ID_UPDATE,
    EVENT_PUSH_BUTTON_UPDATE,
    EVENT_SHUTTER_UPDATE,
    EVENT_THERMOSTAT_UPDATE,
    MaxConnection,
)
from ._vendor.pymaxcul._const import (
    ATTR_BATTERY_LOW,
    ATTR_DEVICE_ID,
    ATTR_DESIRED_TEMPERATURE,
    ATTR_MEASURED_TEMPERATURE,
    ATTR_MODE,
    ATTR_VALVE_POSITION,
    EVENT_CONNECTION_STATUS,
    DEVICE_TYPES_BY_NAME,
    HEATING_THERMOSTAT,
    HEATING_THERMOSTAT_PLUS,
    MODE_MANUAL,
    SHUTTER_CONTACT,
    WALL_MOUNTED_THERMOSTAT,
)
from ._vendor.pymaxcul._messages import (
    AckMessage,
    AddLinkPartnerMessage,
    ConfigWeekProfileMessage,
    RemoveLinkPartnerMessage,
    WallThermostatControlMessage,
    WallThermostatStateMessage,
)

from .week_profile import WeekProfileChunk, compile_week_profile

LOGGER = logging.getLogger(__name__)

DOMAIN = "maxcul"

PLATFORMS = ["climate", "binary_sensor", "switch", "sensor"]
CONNECTION_STARTUP_TIMEOUT = 20

CONF_DEVICE_PATH = "device_path"
CONF_SENDER_ID = "sender_id"
CONF_DEVICE_TYPE_OVERRIDES = "device_type_overrides"

SERVICE_ENABLE_PAIRING = "enable_pairing"
SERVICE_CONF_DEVICE_PATH = "device_path"
SERVICE_CONF_DURATION = "duration"
SERVICE_SET_WEEK_PROFILE = "set_week_profile"
SERVICE_CONF_DEVICE_ID = "device_id"
SERVICE_CONF_AREA_ID = "area_id"
SERVICE_CONF_WEEK_PROFILE = "week_profile"
SERVICE_CONF_REQUEST_ID = "request_id"
CONF_WEEK_PROFILES = "week_profiles"
SERVICE_ASSOCIATE = "associate"
SERVICE_DEASSOCIATE = "deassociate"
SERVICE_CONF_THERMOSTAT_ID = "thermostat_id"
SERVICE_CONF_WINDOW_CONTACT_ID = "window_contact_id"

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

ENABLE_PAIRING_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(SERVICE_CONF_DEVICE_PATH): cv.string,
        vol.Optional(SERVICE_CONF_DURATION, default=30): vol.All(
            cv.positive_int, vol.Range(max=300)
        ),
    }
)


def _validate_week_profile_service_data(data: dict[str, Any]) -> dict[str, Any]:
    """Require exactly one thermostat or area as the profile target."""
    if (SERVICE_CONF_DEVICE_ID in data) == (SERVICE_CONF_AREA_ID in data):
        raise vol.Invalid("Choose either device_id or area_id")
    return data


SET_WEEK_PROFILE_SERVICE_SCHEMA = vol.Schema(
    vol.All(
        {
            vol.Optional(SERVICE_CONF_DEVICE_ID): cv.string,
            vol.Optional(SERVICE_CONF_AREA_ID): cv.string,
            vol.Required(SERVICE_CONF_WEEK_PROFILE): cv.string,
            vol.Optional(SERVICE_CONF_DEVICE_PATH): cv.string,
            vol.Optional(SERVICE_CONF_REQUEST_ID): cv.string,
        },
        _validate_week_profile_service_data,
    )
)


def _validate_association_service_data(data: dict[str, Any]) -> dict[str, Any]:
    """Require exactly one complete association selection mode."""
    single_fields = {
        SERVICE_CONF_THERMOSTAT_ID,
        SERVICE_CONF_WINDOW_CONTACT_ID,
    }
    multi_fields = {"thermostat_ids", "window_contact_ids"}
    present_fields = data.keys()
    single_selected = single_fields <= present_fields
    multi_selected = multi_fields <= present_fields

    if bool(single_fields & present_fields) != single_selected:
        raise vol.Invalid("Both thermostat_id and window_contact_id are required")
    if bool(multi_fields & present_fields) != multi_selected:
        raise vol.Invalid("Both thermostat_ids and window_contact_ids are required")
    if sum((single_selected, multi_selected, "area_id" in present_fields)) != 1:
        raise vol.Invalid(
            "Choose one thermostat/contact pair, lists of both, or one area"
        )
    return data


DEVICE_ASSOCIATION_SERVICE_SCHEMA = vol.Schema(
    vol.All(
        {
            vol.Optional(SERVICE_CONF_THERMOSTAT_ID): cv.string,
            vol.Optional(SERVICE_CONF_WINDOW_CONTACT_ID): cv.string,
            vol.Optional("thermostat_ids"): vol.All(
                cv.ensure_list, [cv.string], vol.Length(min=1)
            ),
            vol.Optional("window_contact_ids"): vol.All(
                cv.ensure_list, [cv.string], vol.Length(min=1)
            ),
            vol.Optional("area_id"): cv.string,
            vol.Optional(SERVICE_CONF_DEVICE_PATH): cv.string,
        },
        _validate_association_service_data,
    )
)

SIGNAL_DEVICE_PAIRED = f"{DOMAIN}.device_paired"
SIGNAL_DEVICE_REPAIRED = f"{DOMAIN}.device_repaired"
SIGNAL_THERMOSTAT_UPDATE = f"{DOMAIN}.thermostat_update"
SIGNAL_SHUTTER_UPDATE = f"{DOMAIN}.shutter_update"
SIGNAL_PUSH_BUTTON_UPDATE = f"{DOMAIN}.push_button_update"
SIGNAL_WEEK_PROFILE_UPDATE = f"{DOMAIN}.week_profile_update"
SIGNAL_FHT_ID_UPDATE = f"{DOMAIN}.fht_id_update"
SIGNAL_CUL_CONNECTION_STATUS = f"{DOMAIN}.cul_connection_status"
EVENT_WEEK_PROFILE_UPDATE = f"{DOMAIN}.week_profile_update"

ATTR_CONNECTION_DEVICE_PATH = "connection_device_path"

type MaxCulConfigEntry = ConfigEntry[MaxCulConnection]


@dataclass
class _WeekProfileRequest:
    """Track acknowledgement state for the chunks of one profile update."""

    device_id: int
    request_group_id: str
    total_chunks: int
    chunk_days: dict[int, int] = field(default_factory=dict)
    acknowledged_days: set[int] = field(default_factory=set)
    pending_counters: set[int] = field(default_factory=set)
    acknowledged_chunks: int = 0
    ready: bool = False
    failed: bool = False


def normalize_device_path(path: str) -> str:
    """Canonicalize serial paths and network addresses for matching."""
    normalized_path = path.strip()
    if normalized_path.casefold().startswith("telnet://"):
        normalized_path = normalized_path[len("telnet://") :]

    if not normalized_path.startswith("/") and ":" in normalized_path:
        host, separator, port = normalized_path.rpartition(":")
        if separator and port.isdecimal():
            return f"{host.casefold()}:{port}"

    return normalized_path


def _find_paired_device_candidates(
    known_devices: dict[str, Any], identifier: str
) -> list[tuple[str, dict[str, Any]]]:
    """Find paired devices by hexadecimal address or saved friendly name."""
    normalized_identifier = identifier.strip().casefold()
    matching_devices: list[tuple[str, dict[str, Any]]] = []

    try:
        numeric_id = int(identifier, 16)
    except ValueError:
        numeric_id = None

    for device_id, device in known_devices.items():
        if not isinstance(device, dict):
            continue
        address_matches = device_id == identifier or (
            numeric_id is not None and device_id == str(numeric_id)
        )
        name = device.get(CONF_NAME)
        name_matches = (
            isinstance(name, str) and name.strip().casefold() == normalized_identifier
        )
        if address_matches or name_matches:
            matching_devices.append((device_id, device))

    return matching_devices


def _resolve_association_entity(
    hass: HomeAssistant, entity_id: str, expected_domain: str
) -> str | None:
    """Resolve a MaxCUL entity selector value to its paired-device ID."""
    entity_entry = er.async_get(hass).async_get(entity_id)
    if entity_entry is None:
        return None
    entity_domain = entity_entry.entity_id.partition(".")[0]
    if entity_entry.platform != DOMAIN or entity_domain != expected_domain:
        raise ServiceValidationError(
            f"Selected entity {entity_id} is not a MaxCUL {expected_domain} entity"
        )
    return entity_entry.unique_id


def register_paired_device(
    hass: HomeAssistant,
    config_entry: MaxCulConfigEntry,
    device_id: str,
    name: str,
    device_type: str,
) -> None:
    """Persist a newly paired device without replacing other entry data."""
    known_devices = config_entry.data.get(CONF_DEVICES) or {}
    device = {CONF_NAME: name, CONF_TYPE: device_type}
    if known_devices.get(device_id) == device:
        return

    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            CONF_DEVICES: {**known_devices, device_id: device},
        },
    )


def get_effective_device_type(
    config_entry: MaxCulConfigEntry, device_id: str | int, reported_type: Any
) -> Any:
    """Return a configured type override, falling back to the radio report."""
    overrides = config_entry.options.get(CONF_DEVICE_TYPE_OVERRIDES)
    if not isinstance(overrides, dict):
        return reported_type

    override = overrides.get(str(device_id))
    if isinstance(override, str) and override in DEVICE_TYPES_BY_NAME:
        return override
    return reported_type


def _store_week_profile_state(
    hass: HomeAssistant,
    config_entry: MaxCulConfigEntry,
    device_id: int,
    profile: str,
    request_id: str,
    status: str,
    request_group_id: str | None = None,
    progress: dict[str, int] | None = None,
) -> None:
    """Persist the latest profile and notify the thermostat entity."""
    device_key = str(device_id)
    LOGGER.debug(
        "Week profile for MAX! thermostat %s (0x%06x) changed to %s "
        "(request %s, connection %s)",
        device_key,
        device_id,
        status,
        request_id,
        config_entry.data[CONF_DEVICE_PATH],
    )
    profiles = config_entry.data.get(CONF_WEEK_PROFILES) or {}
    previous_state = profiles.get(device_key)
    profile_state: dict[str, Any] = {
        "profile": profile,
        "request_id": request_id,
        "status": status,
    }
    if request_group_id is not None:
        profile_state["request_group_id"] = request_group_id
    elif (
        isinstance(previous_state, dict)
        and previous_state.get("request_id") == request_id
    ):
        if previous_state.get("request_group_id") is not None:
            profile_state["request_group_id"] = previous_state["request_group_id"]
    if progress is not None:
        profile_state["progress"] = progress
    elif (
        isinstance(previous_state, dict)
        and previous_state.get("request_id") == request_id
    ):
        if previous_state.get("progress") is not None:
            profile_state["progress"] = previous_state["progress"]
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            CONF_WEEK_PROFILES: {**profiles, device_key: profile_state},
        },
    )
    dispatcher_send(
        hass,
        SIGNAL_WEEK_PROFILE_UPDATE,
        {
            "device_id": device_key,
            ATTR_CONNECTION_DEVICE_PATH: config_entry.data[CONF_DEVICE_PATH],
            **profile_state,
        },
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the MaxCUL integration and register its service actions.

    Service actions are registered here (not in async_setup_entry), so they
    exist even when no config entry is loaded and automations can be validated.
    """

    @callback
    def _service_enable_pairing(service: ServiceCall) -> None:
        service_device_path = service.data.get(SERVICE_CONF_DEVICE_PATH)
        normalized_service_path = (
            normalize_device_path(service_device_path)
            if service_device_path
            else None
        )
        duration = service.data[SERVICE_CONF_DURATION]

        matching_entries = [
            entry
            for entry in hass.config_entries.async_loaded_entries(DOMAIN)
            if normalized_service_path is None
            or (
                entry.data.get(CONF_DEVICE_PATH)
                and normalize_device_path(entry.data[CONF_DEVICE_PATH])
                == normalized_service_path
            )
        ]

        if len(matching_entries) == 1:
            matching_entries[0].runtime_data.enable_pairing(duration)
            return

        if len(matching_entries) > 1:
            raise ServiceValidationError(
                "Multiple MaxCUL connections are loaded; specify device_path"
            )

        raise ServiceValidationError(
            "No loaded MaxCUL connection found"
            if normalized_service_path is None
            else f"No loaded MaxCUL connection found for {service_device_path}"
        )

    @callback
    def _service_set_week_profile(service: ServiceCall) -> None:
        device_identifier = service.data.get(SERVICE_CONF_DEVICE_ID)
        area_id = service.data.get(SERVICE_CONF_AREA_ID)
        target_description = (
            f"area {area_id}"
            if area_id is not None
            else f"thermostat {device_identifier}"
        )
        if device_identifier is not None:
            selected_entity_id = _resolve_association_entity(
                hass, device_identifier, "climate"
            )
            if selected_entity_id is not None:
                device_identifier = selected_entity_id
                target_description = f"thermostat entity {device_identifier}"
        service_device_path = service.data.get(SERVICE_CONF_DEVICE_PATH)
        normalized_service_path = (
            normalize_device_path(service_device_path)
            if service_device_path
            else None
        )
        try:
            chunks = compile_week_profile(service.data[SERVICE_CONF_WEEK_PROFILE])
        except ValueError as err:
            LOGGER.warning(
                "Rejected invalid week profile for %s: %s",
                target_description,
                err,
            )
            raise ServiceValidationError(str(err)) from err
        LOGGER.debug(
            "Received week-profile request for %s (%d encoded chunks)",
            target_description,
            len(chunks),
        )

        matching_entries: list[tuple[MaxCulConfigEntry, int]] = []
        matching_device_found = False
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            entry_path = entry.data.get(CONF_DEVICE_PATH)
            if (
                normalized_service_path is not None
                and (
                    not entry_path
                    or normalize_device_path(entry_path) != normalized_service_path
                )
            ):
                continue

            if area_id is not None:
                device_ids = _association_devices_in_area(
                    hass, entry, area_id, "climate"
                )
            else:
                if device_identifier is None:
                    raise ServiceValidationError(
                        "Choose either a thermostat or an area"
                    )
                known_devices = entry.data.get(CONF_DEVICES) or {}
                candidates = _find_paired_device_candidates(
                    known_devices, device_identifier
                )
                device_ids = [device_id for device_id, _ in candidates]

            for device_id in device_ids:
                device = (entry.data.get(CONF_DEVICES) or {}).get(device_id)
                if not isinstance(device, dict):
                    continue
                matching_device_found = True
                device_type = get_effective_device_type(
                    entry, device_id, device.get(CONF_TYPE)
                )
                if not (
                    isinstance(device_type, str)
                    and "THERMOSTAT" in device_type.upper()
                ):
                    continue
                try:
                    numeric_device_id = int(device_id)
                except ValueError as err:
                    raise ServiceValidationError(
                        f"Invalid stored MAX! device address {device_id}"
                    ) from err
                if not 1 <= numeric_device_id <= 0xFFFFFF:
                    raise ServiceValidationError(
                        f"Invalid stored MAX! device address {device_id}"
                    )
                matching_entries.append((entry, numeric_device_id))

        if area_id is None and len(matching_entries) > 1:
            LOGGER.warning(
                "Week-profile target %s matched multiple thermostats; "
                "specify a device path",
                target_description,
            )
            raise ServiceValidationError(
                "Thermostat identifier matches multiple paired devices or connections; "
                "use its hexadecimal address and specify device_path if needed"
            )
        if not matching_entries:
            LOGGER.warning("No loaded MAX! thermostat found for %s", target_description)
            if matching_device_found:
                raise ServiceValidationError(
                    f"Device {device_identifier} is not a supported thermostat"
                )
            raise ServiceValidationError(
                f"No loaded MaxCUL thermostat found for "
                f"{'area ' + area_id if area_id is not None else 'device ' + device_identifier}"
            )

        LOGGER.debug(
            "Resolved week-profile target %s to MAX! thermostat(s): %s",
            target_description,
            ", ".join(
                f"0x{device_id:06x} via {entry.data.get(CONF_DEVICE_PATH)}"
                for entry, device_id in matching_entries
            ),
        )
        profile = service.data[SERVICE_CONF_WEEK_PROFILE]
        request_group_id = service.data.get(SERVICE_CONF_REQUEST_ID) or uuid4().hex
        send_errors: list[tuple[int, HomeAssistantError]] = []
        for entry, numeric_device_id in matching_entries:
            request_id = f"{request_group_id}:{entry.entry_id}:{numeric_device_id}"
            progress = {
                "acknowledged_chunks": 0,
                "total_chunks": len(chunks),
                "completed_days": 0,
                "total_days": len({chunk.day for chunk in chunks}),
                "current_day": min(chunk.day for chunk in chunks),
            }
            _store_week_profile_state(
                hass,
                entry,
                numeric_device_id,
                profile,
                request_id,
                "pending",
                request_group_id,
                progress,
            )
            try:
                entry.runtime_data.set_week_profile(
                    numeric_device_id, chunks, request_id, request_group_id
                )
            except HomeAssistantError as err:
                _store_week_profile_state(
                    hass,
                    entry,
                    numeric_device_id,
                    profile,
                    request_id,
                    "failed",
                    request_group_id,
                    progress,
                )
                send_errors.append((numeric_device_id, err))

        if send_errors:
            failed_ids = ", ".join(
                f"{device_id:06x}" for device_id, _ in send_errors
            )
            LOGGER.error(
                "Could not queue week profile for MAX! thermostat(s): %s",
                failed_ids,
            )
            raise ServiceValidationError(
                f"Could not queue week profile for MAX! thermostat(s): {failed_ids}"
            ) from send_errors[0][1]

    @callback
    def _service_associate(service: ServiceCall) -> None:
        _service_change_association(service, associate=True)

    @callback
    def _service_deassociate(service: ServiceCall) -> None:
        _service_change_association(service, associate=False)

    @callback
    def _week_profile_status_changed(payload: dict[str, Any]) -> None:
        if not payload.get("status_update"):
            return
        device_id = str(payload[ATTR_DEVICE_ID])
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            entry_path = entry.data.get(CONF_DEVICE_PATH)
            if (
                not entry_path
                or normalize_device_path(entry_path)
                != normalize_device_path(payload[ATTR_CONNECTION_DEVICE_PATH])
            ):
                continue
            profiles = entry.data.get(CONF_WEEK_PROFILES) or {}
            profile_state = profiles.get(device_id)
            if (
                not isinstance(profile_state, dict)
                or profile_state.get("request_id") != payload["request_id"]
            ):
                continue
            _store_week_profile_state(
                hass,
                entry,
                int(device_id),
                profile_state["profile"],
                payload["request_id"],
                payload["status"],
                payload.get("request_group_id"),
                payload.get("progress"),
            )
            break

    def _service_change_association(
        service: ServiceCall, *, associate: bool
    ) -> None:
        service_device_path = service.data.get(SERVICE_CONF_DEVICE_PATH)
        normalized_service_path = (
            normalize_device_path(service_device_path)
            if service_device_path
            else None
        )
        entries = [
            entry
            for entry in hass.config_entries.async_loaded_entries(DOMAIN)
            if normalized_service_path is None
            or (
                entry.data.get(CONF_DEVICE_PATH)
                and normalize_device_path(entry.data[CONF_DEVICE_PATH])
                == normalized_service_path
            )
        ]
        associations: list[tuple[MaxCulConfigEntry, int, str, int]] = []

        if "area_id" in service.data:
            area_id = service.data["area_id"]
            if ar.async_get(hass).async_get_area(area_id) is None:
                raise ServiceValidationError(f"Unknown Home Assistant area {area_id}")

            for entry in entries:
                thermostat_ids = _association_devices_in_area(
                    hass, entry, area_id, "climate"
                )
                window_contact_ids = _association_devices_in_area(
                    hass, entry, area_id, "binary_sensor"
                )
                for thermostat_id in thermostat_ids:
                    for window_contact_id in window_contact_ids:
                        associations.append(
                            _resolve_association_pair(
                                thermostat_id,
                                window_contact_id,
                                [entry],
                            )
                        )
            if not associations:
                raise ServiceValidationError(
                    f"Area {area_id} must contain a supported MAX! thermostat "
                    "and window contact on the same loaded connection"
                )
        else:
            if "thermostat_ids" in service.data:
                thermostat_identifiers = [
                    _resolve_selected_association_entity(hass, entity_id, "climate")
                    for entity_id in service.data["thermostat_ids"]
                ]
                window_contact_identifiers = [
                    _resolve_selected_association_entity(
                        hass, entity_id, "binary_sensor"
                    )
                    for entity_id in service.data["window_contact_ids"]
                ]
            else:
                thermostat_identifier = service.data[SERVICE_CONF_THERMOSTAT_ID]
                window_contact_identifier = service.data[
                    SERVICE_CONF_WINDOW_CONTACT_ID
                ]
                thermostat_entity_id = _resolve_association_entity(
                    hass, thermostat_identifier, "climate"
                )
                window_contact_entity_id = _resolve_association_entity(
                    hass, window_contact_identifier, "binary_sensor"
                )
                thermostat_identifiers = [
                    thermostat_entity_id or thermostat_identifier
                ]
                window_contact_identifiers = [
                    window_contact_entity_id or window_contact_identifier
                ]

            for thermostat_identifier in dict.fromkeys(thermostat_identifiers):
                for window_contact_identifier in dict.fromkeys(
                    window_contact_identifiers
                ):
                    associations.append(
                        _resolve_association_pair(
                            thermostat_identifier,
                            window_contact_identifier,
                            entries,
                        )
                    )

        for (
            entry,
            thermostat_id,
            thermostat_type,
            window_contact_id,
        ) in associations:
            entry.runtime_data.set_device_association(
                thermostat_id,
                thermostat_type,
                window_contact_id,
                associate=associate,
            )

    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                f"/{DOMAIN}/week-profile-card.js",
                str(Path(__file__).parent / "www" / "week-profile-card.js"),
                cache_headers=False,
            )
        ]
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ENABLE_PAIRING,
        _service_enable_pairing,
        schema=ENABLE_PAIRING_SERVICE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_WEEK_PROFILE,
        _service_set_week_profile,
        schema=SET_WEEK_PROFILE_SERVICE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ASSOCIATE,
        _service_associate,
        schema=DEVICE_ASSOCIATION_SERVICE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DEASSOCIATE,
        _service_deassociate,
        schema=DEVICE_ASSOCIATION_SERVICE_SCHEMA,
    )
    async_dispatcher_connect(
        hass, SIGNAL_WEEK_PROFILE_UPDATE, _week_profile_status_changed
    )

    return True


def _association_thermostat_type(device_type: object) -> str | None:
    """Return a MAX! protocol type for a supported thermostat model."""
    if not isinstance(device_type, str):
        return None
    normalized = "".join(char for char in device_type.casefold() if char.isalnum())
    return {
        "heatingthermostat": HEATING_THERMOSTAT,
        "heatingthermostatplus": HEATING_THERMOSTAT_PLUS,
        "wallmountedthermostat": WALL_MOUNTED_THERMOSTAT,
        "wallthermostatplus": WALL_MOUNTED_THERMOSTAT,
    }.get(normalized)


def _is_window_contact_type(device_type: object) -> bool:
    """Return True for a MAX! shutter/window contact."""
    if not isinstance(device_type, str):
        return False
    normalized = "".join(char for char in device_type.casefold() if char.isalnum())
    return normalized in {"shuttercontact", "windowcontact", "windowshuttercontact"}


def _resolve_selected_association_entity(
    hass: HomeAssistant, entity_id: str, expected_domain: str
) -> str:
    """Resolve and validate an entity selected for a multi-device association."""
    device_id = _resolve_association_entity(hass, entity_id, expected_domain)
    if device_id is None:
        raise ServiceValidationError(
            f"Selected entity {entity_id} is not a registered MaxCUL {expected_domain} entity"
        )
    return device_id


def _association_devices_in_area(
    hass: HomeAssistant,
    config_entry: MaxCulConfigEntry,
    area_id: str,
    expected_domain: str,
) -> list[str]:
    """Return supported paired devices of one entity domain assigned to an area."""
    known_devices = config_entry.data.get(CONF_DEVICES) or {}
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    matching_device_ids = []

    for entity in entity_registry.entities.values():
        if (
            entity.platform != DOMAIN
            or entity.entity_id.partition(".")[0] != expected_domain
        ):
            continue

        device_id = entity.unique_id
        device = known_devices.get(device_id)
        if not isinstance(device, dict):
            continue
        device_type = get_effective_device_type(
            config_entry, device_id, device.get(CONF_TYPE)
        )
        if expected_domain == "climate":
            if _association_thermostat_type(device_type) is None:
                continue
        elif not _is_window_contact_type(device_type):
            continue

        device_entry = (
            device_registry.devices.get(entity.device_id)
            if entity.device_id is not None
            else None
        )
        assigned_area_id = entity.area_id or (
            device_entry.area_id if device_entry is not None else None
        )
        if assigned_area_id == area_id:
            matching_device_ids.append(device_id)

    return list(dict.fromkeys(matching_device_ids))


def _resolve_association_pair(
    thermostat_identifier: str,
    window_contact_identifier: str,
    entries: list[MaxCulConfigEntry],
) -> tuple[MaxCulConfigEntry, int, str, int]:
    """Resolve one association pair to a loaded connection and protocol type."""
    matching_associations = []
    unsupported_thermostat = False
    unsupported_window_contact = False

    for entry in entries:
        known_devices = entry.data.get(CONF_DEVICES) or {}
        thermostats = _find_paired_device_candidates(
            known_devices, thermostat_identifier
        )
        window_contacts = _find_paired_device_candidates(
            known_devices, window_contact_identifier
        )
        if not thermostats or not window_contacts:
            continue

        for thermostat_id, thermostat in thermostats:
            thermostat_type = _association_thermostat_type(
                get_effective_device_type(
                    entry, thermostat_id, thermostat.get(CONF_TYPE)
                )
            )
            if thermostat_type is None:
                unsupported_thermostat = True
                continue

            for window_contact_id, window_contact in window_contacts:
                if not _is_window_contact_type(
                    get_effective_device_type(
                        entry,
                        window_contact_id,
                        window_contact.get(CONF_TYPE),
                    )
                ):
                    unsupported_window_contact = True
                    continue
                try:
                    numeric_thermostat_id = int(thermostat_id)
                    numeric_window_contact_id = int(window_contact_id)
                except ValueError as err:
                    raise ServiceValidationError(
                        "A paired MAX! device has an invalid stored address"
                    ) from err

                if any(
                    device_id < 1 or device_id > 0xFFFFFF
                    for device_id in (
                        numeric_thermostat_id,
                        numeric_window_contact_id,
                    )
                ):
                    raise ServiceValidationError(
                        "MAX! device addresses must be between 000001 and FFFFFF"
                    )
                if numeric_thermostat_id == numeric_window_contact_id:
                    continue
                matching_associations.append(
                    (
                        entry,
                        numeric_thermostat_id,
                        thermostat_type,
                        numeric_window_contact_id,
                    )
                )

    if len(matching_associations) > 1:
        raise ServiceValidationError(
            "Device identifiers match multiple paired devices or connections; "
            "use hexadecimal addresses and specify device_path if needed"
        )
    if matching_associations:
        return matching_associations[0]
    if unsupported_thermostat:
        raise ServiceValidationError(
            f"{thermostat_identifier} is not a supported heating thermostat"
        )
    if unsupported_window_contact:
        raise ServiceValidationError(
            f"{window_contact_identifier} is not a supported window contact"
        )
    raise ServiceValidationError(
        "Could not find both paired devices on the same loaded MaxCUL connection"
    )


async def async_setup_entry(
    hass: HomeAssistant, config_entry: MaxCulConfigEntry
) -> bool:
    """Set up MaxCUL from a config entry."""
    previous_options = dict(config_entry.options)

    async def options_updated(
        hass: HomeAssistant, updated_entry: MaxCulConfigEntry
    ) -> None:
        nonlocal previous_options
        current_options = dict(updated_entry.options)
        if current_options == previous_options:
            return
        previous_options = current_options
        LOGGER.debug("Reloading MaxCUL because integration options changed")
        await hass.config_entries.async_reload(updated_entry.entry_id)

    config_entry.async_on_unload(config_entry.add_update_listener(options_updated))
    device_path: str = config_entry.data[CONF_DEVICE_PATH]
    sender_id = config_entry.options.get(CONF_SENDER_ID)

    connection = MaxCulConnection(
        hass,
        device_path=device_path,
        sender_id=sender_id,
        config_entry_id=config_entry.entry_id,
    )
    connection.start()
    config_entry.runtime_data = connection
    connected = await hass.async_add_executor_job(
        connection.wait_for_initial_connection, CONNECTION_STARTUP_TIMEOUT
    )
    if not connected:
        connection_error = connection.connection_error
        await hass.async_add_executor_job(connection.stop)
        raise ConfigEntryNotReady(
            connection_error
            or f"No valid CUL firmware response received from {device_path}"
        )

    # Remove devices from the registry that are no longer part of the entry.
    known_devices = config_entry.data.get(CONF_DEVICES) or {}
    known_device_ids = {str(device_id) for device_id in known_devices}
    device_registry = dr.async_get(hass)
    for device_entry in dr.async_entries_for_config_entry(
        device_registry, config_entry.entry_id
    ):
        if any(
            identifier_domain == DOMAIN and device_id not in known_device_ids
            for identifier_domain, device_id in device_entry.identifiers
        ):
            device_registry.async_remove_device(device_entry.id)

    try:
        await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)
    except (Exception, asyncio.CancelledError):
        # Do not leave the worker thread running if a platform fails to load.
        try:
            await hass.async_add_executor_job(connection.stop)
        except Exception:
            LOGGER.exception("Failed to stop MaxCUL connection after setup failure")
        raise

    connection.mark_setup_complete()
    return True


async def async_unload_entry(
    hass: HomeAssistant, config_entry: MaxCulConfigEntry
) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry, PLATFORMS
    )

    if unload_ok:
        config_entry.runtime_data.dismiss_connection_notification()
        # Stopping joins the worker thread, so keep it off the event loop.
        await hass.async_add_executor_job(config_entry.runtime_data.stop)

    return unload_ok


class MaxCulConnection:
    """Wrapper around the maxcul connection thread."""

    def __init__(
        self,
        hass: HomeAssistant,
        device_path: str,
        sender_id: int | None = None,
        config_entry_id: str | None = None,
    ) -> None:
        self._hass = hass
        self._device_path = device_path
        self._config_entry_id = config_entry_id or re.sub(
            r"[^a-zA-Z0-9_]", "_", normalize_device_path(device_path)
        )
        self._connection_notification_id = (
            f"{DOMAIN}_connection_{self._config_entry_id}"
        )
        self._connection_ready = threading.Event()
        self._connection_status: bool | None = None
        self._connection_error: str | None = None
        self._setup_complete = False

        if sender_id is None:
            sender_id = 0x123456

        self._connection = MaxConnection(
            device_path=device_path,
            callback=self._callback,
            sender_id=sender_id,
        )
        self._week_profile_requests: dict[str, _WeekProfileRequest] = {}
        self._week_profile_counters: dict[int, str] = {}
        original_handle_message = self._connection._handle_message
        original_resend_message = self._connection._resend_message

        def handle_message(message, signal_strength) -> None:
            original_handle_message(message, signal_strength)
            if isinstance(message, AckMessage):
                self._handle_week_profile_ack(message)
            elif isinstance(
                message, (WallThermostatControlMessage, WallThermostatStateMessage)
            ):
                payload = {ATTR_DEVICE_ID: message.sender_id}
                for attribute, message_attribute in (
                    (ATTR_MEASURED_TEMPERATURE, "temperature"),
                    (ATTR_DESIRED_TEMPERATURE, "desired_temperature"),
                    (ATTR_MODE, "mode"),
                    (ATTR_VALVE_POSITION, "valve_position"),
                    (ATTR_BATTERY_LOW, "battery_low"),
                ):
                    value = getattr(message, message_attribute, None)
                    if value is not None:
                        payload[attribute] = value
                self._callback(EVENT_THERMOSTAT_UPDATE, payload)

        def resend_message() -> None:
            original_resend_message()
            self._check_week_profile_timeouts()

        self._connection._handle_message = handle_message
        self._connection._resend_message = resend_message

    def start(self) -> None:
        """Start the connection thread."""
        self._connection.start()

    def stop(self) -> None:
        """Stop the connection thread."""
        self._connection.stop()

    @property
    def connection_error(self) -> str | None:
        """Return the last reason the CUL could not be reached."""
        return self._connection_error

    @property
    def fht_id(self) -> str | None:
        """Return the FHT ID reported by the CUL."""
        return self._connection.fht_id

    @property
    def cul_version(self) -> str | None:
        """Return the CUL firmware version response."""
        return self._connection.cul_version

    @property
    def hardware_revision(self) -> str | None:
        """Return the CUL hardware revision, if reported."""
        return self._connection.hardware_revision

    def wait_for_initial_connection(self, timeout: float) -> bool:
        """Wait for the first CUL reachability result."""
        if not self._connection_ready.wait(timeout):
            return False
        return self._connection_status is True

    def mark_setup_complete(self) -> None:
        """Enable runtime notifications after entity setup succeeds."""
        self._setup_complete = True
        if self._connection_status is False:
            self._hass.async_create_task(
                self._async_handle_connection_status(
                    False, self._connection_error
                )
            )

    def dismiss_connection_notification(self) -> None:
        """Remove a previous connection warning."""
        persistent_notification.async_dismiss(
            self._hass, self._connection_notification_id
        )

    async def _async_handle_connection_status(
        self, connected: bool, error: str | None
    ) -> None:
        if connected:
            self.dismiss_connection_notification()
            return
        if not self._setup_complete:
            return

        retry_message = (
            "CulMax is attempting to reconnect."
            if error == "CUL connection lost; reconnecting"
            else "Repeated reconnection attempts failed. Reload the CulMax "
            "integration after restoring access to the CUL."
        )
        message = (
            f"CulMax cannot communicate with the CUL at {self._device_path}. "
            f"{error or 'No valid firmware response was received.'} "
            "Check that the device is connected and that no other application "
            "or Docker container is using its serial port. "
            f"{retry_message}"
        )
        persistent_notification.async_create(
            self._hass,
            message,
            title="CulMax CUL connection lost",
            notification_id=self._connection_notification_id,
        )

    def _callback(self, event, payload) -> None:
        """Handle events from the connection thread (not the event loop).

        dispatcher_send is thread-safe and may be used from here.
        """
        if event == EVENT_CONNECTION_STATUS:
            connected = payload["connected"]
            error = payload.get("error")
            self._connection_status = connected
            self._connection_error = error
            self._connection_ready.set()
            dispatcher_send(
                self._hass,
                SIGNAL_CUL_CONNECTION_STATUS,
                {**payload, ATTR_CONNECTION_DEVICE_PATH: self._device_path},
            )
            self._hass.add_job(
                self._async_handle_connection_status, connected, error
            )

        elif event == EVENT_DEVICE_PAIRED:
            payload[ATTR_CONNECTION_DEVICE_PATH] = self._device_path
            dispatcher_send(self._hass, SIGNAL_DEVICE_PAIRED, payload)

        elif event == EVENT_DEVICE_REPAIRED:
            payload[ATTR_CONNECTION_DEVICE_PATH] = self._device_path
            dispatcher_send(self._hass, SIGNAL_DEVICE_REPAIRED, payload)

        elif event == EVENT_THERMOSTAT_UPDATE:
            dispatcher_send(self._hass, SIGNAL_THERMOSTAT_UPDATE, payload)

        elif event == EVENT_SHUTTER_UPDATE:
            dispatcher_send(self._hass, SIGNAL_SHUTTER_UPDATE, payload)

        elif event == EVENT_PUSH_BUTTON_UPDATE:
            dispatcher_send(self._hass, SIGNAL_PUSH_BUTTON_UPDATE, payload)

        elif event == EVENT_FHT_ID_UPDATE:
            payload[ATTR_CONNECTION_DEVICE_PATH] = self._device_path
            dispatcher_send(self._hass, SIGNAL_FHT_ID_UPDATE, payload)

        elif event == EVENT_WEEK_PROFILE_UPDATE:
            payload[ATTR_CONNECTION_DEVICE_PATH] = self._device_path
            payload["status_update"] = True
            dispatcher_send(self._hass, SIGNAL_WEEK_PROFILE_UPDATE, payload)

    @staticmethod
    def _week_profile_progress(request: _WeekProfileRequest) -> dict[str, int]:
        pending_days = {
            request.chunk_days[counter]
            for counter in request.pending_counters
            if counter in request.chunk_days
        }
        incomplete_days = [
            day
            for day in set(request.chunk_days.values())
            if day not in request.acknowledged_days
        ]
        return {
            "acknowledged_chunks": request.acknowledged_chunks,
            "total_chunks": request.total_chunks,
            "completed_days": len(request.acknowledged_days),
            "total_days": len(set(request.chunk_days.values())),
            "current_day": min(pending_days or incomplete_days, default=0),
        }

    def _publish_week_profile_progress(self, request_id: str) -> None:
        request = self._week_profile_requests.get(request_id)
        if request is None:
            return
        self._callback(
            EVENT_WEEK_PROFILE_UPDATE,
            {
                ATTR_DEVICE_ID: request.device_id,
                "request_id": request_id,
                "request_group_id": request.request_group_id,
                "status": "pending",
                "progress": self._week_profile_progress(request),
            },
        )

    def _handle_week_profile_ack(self, message: AckMessage) -> None:
        request_id = self._week_profile_counters.get(message.counter)
        if request_id is None:
            return

        request = self._week_profile_requests.get(request_id)
        if request is None or message.sender_id != request.device_id:
            return

        self._week_profile_counters.pop(message.counter, None)
        request.pending_counters.discard(message.counter)
        day = request.chunk_days.get(message.counter)
        if message.state == "ok":
            request.acknowledged_chunks += 1
            if request.ready and day is not None and not any(
                request.chunk_days.get(counter) == day
                for counter in request.pending_counters
            ):
                request.acknowledged_days.add(day)
        if request.ready:
            self._publish_week_profile_progress(request_id)
        LOGGER.debug(
            "Received week-profile ACK from MAX! thermostat 0x%06x "
            "for counter %s: %s (%d counters pending, request %s)",
            request.device_id,
            message.counter,
            message.state,
            len(request.pending_counters),
            request_id,
        )
        if message.state != "ok":
            request.failed = True
        if request.ready and request.failed:
            self._finish_week_profile(request_id, "failed")
        elif request.ready and not request.pending_counters:
            self._finish_week_profile(request_id, "acknowledged")

    def _check_week_profile_timeouts(self) -> None:
        outstanding = self._connection._outstanding_acks
        for request_id, request in tuple(self._week_profile_requests.items()):
            expired_counters = [
                counter
                for counter in request.pending_counters
                if counter not in outstanding
            ]
            if request.ready and expired_counters:
                LOGGER.warning(
                    "Timed out waiting for week-profile ACK from MAX! thermostat "
                    "0x%06x for counter(s) %s (request %s)",
                    request.device_id,
                    ", ".join(str(counter) for counter in expired_counters),
                    request_id,
                )
                self._finish_week_profile(request_id, "failed")

    def _finish_week_profile(self, request_id: str, status: str) -> None:
        request = self._week_profile_requests.get(request_id)
        if request is None:
            return
        self._callback(
            EVENT_WEEK_PROFILE_UPDATE,
            {
                ATTR_DEVICE_ID: request.device_id,
                "request_id": request_id,
                "request_group_id": request.request_group_id,
                "status": status,
                "progress": self._week_profile_progress(request),
            },
        )
        self._week_profile_requests.pop(request_id, None)
        for counter in request.pending_counters:
            self._week_profile_counters.pop(counter, None)
        LOGGER.debug(
            "Finished week-profile request %s for MAX! thermostat 0x%06x: %s",
            request_id,
            request.device_id,
            status,
        )

    def enable_pairing(self, duration: int) -> None:
        """Enable pairing of devices."""
        LOGGER.debug("Enabling pairing on %s for %s s", self._device_path, duration)
        self._connection.enable_pairing(duration)

    def add_paired_device(self, device_id: int) -> None:
        """Add a device id to the paired devices."""
        LOGGER.debug("Adding device %s (0x%x)", device_id, device_id)
        self._connection.add_paired_device(device_id)

    def set_temperature(
        self, device_id: int, target_temperature: float, mode
    ) -> None:
        """Set the target temperature of a thermostat device."""
        LOGGER.debug(
            "Setting temperature on %s to %s (mode: %s)",
            device_id,
            target_temperature,
            mode,
        )
        self._connection.set_temperature(device_id, target_temperature, mode)

    def set_plug_adapter_state(self, device_id: int, is_on: bool) -> None:
        """Set a Plug Adapter relay using MAX!'s on/off temperature sentinels."""
        target_temperature = 30.5 if is_on else 4.5
        if not self._connection.set_temperature(
            device_id, target_temperature, MODE_MANUAL
        ):
            raise HomeAssistantError(
                f"Could not queue state change for MAX! Plug Adapter {device_id}"
            )

    def set_week_profile(
        self,
        device_id: int,
        chunks: list[WeekProfileChunk],
        request_id: str,
        request_group_id: str | None = None,
    ) -> None:
        """Send an encoded week profile directly to a thermostat."""
        LOGGER.debug(
            "Sending week profile to MAX! thermostat 0x%06x via %s "
            "(%d chunks, request %s)",
            device_id,
            self._device_path,
            len(chunks),
            request_id,
        )
        request = _WeekProfileRequest(
            device_id=device_id,
            request_group_id=request_group_id or request_id,
            total_chunks=len(chunks),
        )
        self._week_profile_requests[request_id] = request
        for chunk in chunks:
            message = ConfigWeekProfileMessage(
                counter=self._connection._next_counter(),
                sender_id=self._connection.sender_id,
                receiver_id=device_id,
            )
            message.encode_payload = lambda payload=chunk.payload: payload
            request.pending_counters.add(message.counter)
            request.chunk_days[message.counter] = chunk.day
            self._week_profile_counters[message.counter] = request_id
            if not self._connection._send_message(message):
                LOGGER.error(
                    "Could not queue week-profile chunk for MAX! thermostat "
                    "0x%06x (day %s, part %s, counter %s, request %s)",
                    device_id,
                    chunk.day,
                    chunk.part,
                    message.counter,
                    request_id,
                )
                self._finish_week_profile(request_id, "failed")
                raise HomeAssistantError(
                    f"Could not queue week profile for MAX! device {device_id}"
                )
            LOGGER.debug(
                "Queued week-profile chunk for MAX! thermostat 0x%06x "
                "(day %s, part %s, counter %s, request %s)",
                device_id,
                chunk.day,
                chunk.part,
                message.counter,
                request_id,
            )
            self._connection._await_ack(message)
        request.ready = True
        request.acknowledged_days.update(
            day
            for day in set(request.chunk_days.values())
            if not any(
                request.chunk_days.get(counter) == day
                for counter in request.pending_counters
            )
        )
        self._publish_week_profile_progress(request_id)
        if request.failed:
            self._finish_week_profile(request_id, "failed")
        elif not request.pending_counters:
            self._finish_week_profile(request_id, "acknowledged")

    def set_device_association(
        self,
        thermostat_id: int,
        thermostat_type: str,
        window_contact_id: int,
        *,
        associate: bool,
    ) -> None:
        """Create or remove the two MAX! peer links for a thermostat/contact pair."""
        message_type = AddLinkPartnerMessage if associate else RemoveLinkPartnerMessage
        links = (
            (
                thermostat_id,
                window_contact_id,
                SHUTTER_CONTACT,
            ),
            (
                window_contact_id,
                thermostat_id,
                thermostat_type,
            ),
        )

        for device_id, partner_id, partner_type in links:
            partner_address: int | str = (
                partner_id
                if associate
                else f"{partner_id:06X}"
            )
            message = message_type(
                counter=self._connection._next_counter(),
                sender_id=self._connection.sender_id,
                receiver_id=device_id,
                assocDevice=partner_address,
                assocDeviceType=partner_type,
            )
            if not associate:
                # pymaxcul's RemoveLinkPartnerMessage encoder uses DEVICE_TYPES
                # in the wrong direction, so provide the valid wire payload.
                payload = (
                    f"{partner_id:06X}{DEVICE_TYPES_BY_NAME[partner_type]:02X}"
                )
                message.encode_payload = lambda payload=payload: payload
            if not self._connection._send_message(message):
                operation = "associate" if associate else "deassociate"
                raise HomeAssistantError(
                    f"Could not {operation} MAX! devices {thermostat_id} "
                    f"and {window_contact_id}; the operation may be partial"
                )
            self._connection._await_ack(message)
