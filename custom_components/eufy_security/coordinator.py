import logging

import aiohttp
import asyncio
from datetime import timedelta
import json

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import (
    DOMAIN,
    PLATFORMS,
    MESSAGE_IDS_TO_PROCESS,
    MESSAGE_TYPES_TO_PROCESS,
    POLL_REFRESH_MESSAGE,
    EVENT_TYPE_CONFIGURATION,
    START_LISTENING_MESSAGE,
    SET_RTSP_STREAM_MESSAGE,
    GET_PROPERTIES_MESSAGE,
    GET_PROPERTIES_METADATA_MESSAGE,
    SET_RTSP_STREAM_MESSAGE,
)
from .generated import DeviceType
from .websocket import EufySecurityWebSocket

_LOGGER: logging.Logger = logging.getLogger(__package__)

DELAY_FOR_POLLING = 2


class EufySecurityDataUpdateCoordinator(DataUpdateCoordinator):
    def __init__(
        self,
        hass: HomeAssistant,
        update_interval: int,
        host: str,
        port: int,
        session: aiohttp.ClientSession,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=update_interval),
        )
        self.ws = None
        self.host = host
        self.port = port
        self.session = session
        self.platforms = []
        self.data = {}
        self.data["cache"] = {}
        self.data["data"] = {}
        self.start_listening_state = False
        self.poll_refresing_state = False

    async def initialize_ws(self) -> bool:
        self.ws: EufySecurityWebSocket = EufySecurityWebSocket(
            self.host,
            self.port,
            self.session,
            self.on_open,
            self.on_message,
            self.on_close,
            self.on_error,
        )
        await self.ws.set_ws()
        await self.async_start_listening()
        if await self.check_if_started_listening(True) == False:
            _LOGGER.debug(f"{DOMAIN} - check_if_started_listening - returned False")
            raise Exception("Start Listening was not completed in timely manner")

    async def set_start_listening_state(self, value: bool):
        lock = asyncio.Lock()
        async with lock:
            self.start_listening_state = value

    async def check_if_started_listening(self, to_be_value: bool):
        counter = 0
        lock = asyncio.Lock()
        _LOGGER.debug(
            f"{DOMAIN} - check_if_started_listening - {self.start_listening_state} {to_be_value}"
        )
        async with lock:
            while self.start_listening_state != to_be_value:
                await asyncio.sleep(1)
                counter = counter + 1
                _LOGGER.debug(
                    f"{DOMAIN} - check_if_started_listening - {self.start_listening_state} {to_be_value} {counter}"
                )
                if counter > 5:
                    return False
            return True

    async def set_poll_refreshing_state(self, value: bool):
        lock = asyncio.Lock()
        async with lock:
            self.poll_refresing_state = value

    async def check_if_poll_refreshed(self, to_be_value: bool):
        counter = 0
        lock = asyncio.Lock()
        async with lock:
            while self.poll_refresing_state != to_be_value:
                await asyncio.sleep(1)
                counter = counter + 1
                if counter > 5:
                    return False
            return True

    async def on_message(self, message):
        payload = message.json()
        message_type: str = payload["type"]

        if not message_type in MESSAGE_TYPES_TO_PROCESS:
            return

        message = payload[message_type]
        if message_type == "result":
            message_id: str = payload["messageId"]
            if not message_id in MESSAGE_IDS_TO_PROCESS:
                return

            if message_id == "start_listening":
                self.data["data"] = message["state"]
                await self.set_start_listening_state(True)

            if message_id == "poll_refresh":
                await self.set_poll_refreshing_state(True)
                return

        if message_type == "event":
            event_type = message["event"]
            event_sources = message["source"] + "s"
            event_serial_number = message["serialNumber"]
            if not event_type in EVENT_TYPE_CONFIGURATION.keys():
                return

            event_property_name = message.get(
                "name", EVENT_TYPE_CONFIGURATION[event_type]["name"]
            )
            event_property_value = message[
                EVENT_TYPE_CONFIGURATION[event_type]["value"]
            ]
            event_data_is_cached = EVENT_TYPE_CONFIGURATION[event_type]["is_cached"]

            if event_data_is_cached == True:
                self.set_cache_value_for_property(
                    event_sources,
                    event_serial_number,
                    event_property_name,
                    event_property_value,
                )
            else:
                self.set_data_value_for_property(
                    event_sources,
                    event_serial_number,
                    event_property_name,
                    event_property_value,
                )

        self.async_set_updated_data(self.data)

    def set_data_value_for_property(
        self,
        sources: str,
        serial_number: str,
        property_name: str,
        value: str,
    ):
        for entity in self.data["data"][sources]:
            if entity["serialNumber"] == serial_number:
                entity[property_name] = value
                _LOGGER.debug(
                    f"{DOMAIN} - set_event_for_entity -{serial_number} {property_name} {value}"
                )
                break

    def set_cache_value_for_property(
        self,
        sources: str,
        serial_number: str,
        property_name: str,
        value,
    ):
        if isinstance(value, str):
            value = value.replace("\x00", "")

        if self.data["cache"].get(serial_number, None) is None:
            self.data["cache"][serial_number] = {}
        self.data["cache"][serial_number][property_name] = value

    async def on_open(self):
        _LOGGER.debug(f"{DOMAIN} - on_open - executed")

    async def on_close(self):
        await self.set_start_listening_state(False)
        _LOGGER.debug(f"{DOMAIN} - on_close - executed")

    async def on_error(self, message):
        await self.set_start_listening_state(False)
        _LOGGER.debug(f"{DOMAIN} - on_error - executed - {message}")

    async def _async_update_data(self):
        try:
            await self.async_poll_refresh()
            if await self.check_if_poll_refreshed(True) == False:
                _LOGGER.debug(f"{DOMAIN} - check_if_poll_refreshed - returned False")
                raise Exception("Refresh Poll was not completed in timely manner")
            return self.data
        except Exception as exception:
            raise UpdateFailed() from exception

    async def async_send_message(self, message):
        await self.ws.send_message(message)

    async def async_start_listening(self):
        await self.set_start_listening_state(False)
        await self.async_send_message(json.dumps(START_LISTENING_MESSAGE))

    async def async_poll_refresh(self):
        await self.set_poll_refreshing_state(False)
        await self.async_send_message(json.dumps(POLL_REFRESH_MESSAGE))

    async def async_get_properties_metadata_for_device(
        self, device_type: DeviceType, serial_no: str
    ):
        message = GET_PROPERTIES_METADATA_MESSAGE
        message["command"] = message["command"].format(
            self.get_device_type_name(device_type)
        )
        message["serialNumber"] = serial_no
        await self.async_send_message(message)

    async def async_get_properties_for_device(
        self, device_type: DeviceType, serial_no: str
    ):
        message = GET_PROPERTIES_MESSAGE
        message["command"] = message["command"].format(
            self.get_device_type_name(device_type)
        )
        message["serialNumber"] = serial_no
        await self.async_send_message(message)

    async def async_set_rtsp(self, serial_no: str, value: bool):
        message = SET_RTSP_STREAM_MESSAGE
        message["serialNumber"] = serial_no
        message["value"] = value
        await self.async_send_message(json.dumps(message))

    def get_device_type_name(
        self, device_type: DeviceType
    ):  # pylint: disable=no-member
        if device_type == DeviceType.STATION:
            return "station"
        return "device"