"""Support for ICS Calendar."""
import copy
import logging
from datetime import datetime, timedelta

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.calendar import (
    ENTITY_ID_FORMAT,
    PLATFORM_SCHEMA,
    CalendarEventDevice,
    extract_offset,
    get_date,
    is_offset_reached,
)
from homeassistant.const import (
    CONF_NAME,
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import generate_entity_id
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import Throttle
from homeassistant.util.dt import now as hanow

from .calendardata import CalendarData
from .icalendarparser import ICalendarParser

_LOGGER = logging.getLogger(__name__)

CONF_DEVICE_ID = "device_id"
CONF_CALENDARS = "calendars"
CONF_DAYS = "days"
CONF_CALENDAR = "calendar"
CONF_INCLUDE_ALL_DAY = "include_all_day"
CONF_INCLUDE_ALL_DAY2 = "includeAllDay"
CONF_PARSER = "parser"
CONF_DOWNLOAD_INTERVAL = "download_interval"

OFFSET = "!!"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        # pylint: disable=no-value-for-parameter
        vol.Optional(CONF_CALENDARS, default=[]): vol.All(
            cv.ensure_list,
            vol.Schema(
                [
                    vol.Schema(
                        {
                            vol.Required(CONF_URL): vol.Url(),
                            vol.Required(CONF_NAME): cv.string,
                            vol.Optional(
                                CONF_INCLUDE_ALL_DAY, default=False
                            ): cv.boolean,
                            vol.Optional(
                                CONF_INCLUDE_ALL_DAY2, default=False
                            ): cv.boolean,
                            vol.Optional(CONF_USERNAME, default=""): cv.string,
                            vol.Optional(CONF_PASSWORD, default=""): cv.string,
                            vol.Optional(
                                CONF_PARSER, default="rie"
                            ): cv.string,
                            vol.Optional(
                                CONF_DAYS, default=1
                            ): cv.positive_int,
                            vol.Optional(
                                CONF_DOWNLOAD_INTERVAL, default=15
                            ): cv.positive_int,
                        }
                    )
                ]
            ),
        )
    }
)

MIN_TIME_BETWEEN_UPDATES = timedelta(minutes=15)


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    _=None,
):
    """Set up ics_calendar platform.

    :param hass: Home Assistant object
    :type hass: HomeAssistant
    :param config: Config information for the platform
    :type config: ConfigType
    :param add_entities: Callback to add entities to HA
    :type add_entities: AddEntitiesCallback
    :param _: DiscoveryInfo, not used
    :type _: DiscoveryInfoType | None, optional
    """
    _LOGGER.debug("Setting up ics calendars")
    calendar_devices = []
    for calendar in config.get(CONF_CALENDARS):
        device_data = {
            CONF_NAME: calendar.get(CONF_NAME),
            CONF_URL: calendar.get(CONF_URL),
            CONF_INCLUDE_ALL_DAY: calendar.get(CONF_INCLUDE_ALL_DAY),
            CONF_USERNAME: calendar.get(CONF_USERNAME),
            CONF_PASSWORD: calendar.get(CONF_PASSWORD),
            CONF_PARSER: calendar.get(CONF_PARSER),
            CONF_DAYS: calendar.get(CONF_DAYS),
        }
        device_id = f"{device_data[CONF_NAME]}"
        entity_id = generate_entity_id(ENTITY_ID_FORMAT, device_id, hass=hass)
        calendar_devices.append(ICSCalendarEventDevice(entity_id, device_data))

    add_entities(calendar_devices)


class ICSCalendarEventDevice(CalendarEventDevice):  # pylint: disable=R0902
    """A device for getting the next Task from an ICS Calendar."""

    def __init__(self, entity_id: str, device_data):
        """Construct ICSCalendarEventDevice.

        :param entity_id: Entity id for the calendar
        :type entity_id: str
        :param device_data: dict describing the calendar
        :type device_data: dict
        """
        _LOGGER.debug("Initializing calendar: %s", device_data[CONF_NAME])
        self.data = ICSCalendarData(device_data)
        self.entity_id = entity_id
        self._event = None
        self._name = device_data[CONF_NAME]
        self._offset_reached = False
        self._last_call = None
        self._last_event_list = None

    @property
    def event(self):
        """Return the current event for the calendar entity or None.

        :return: The current event as a dict
        :rtype: dict
        """
        return self._event

    @property
    def name(self):
        """Return the name of the calendar."""
        return self._name

    @property
    def should_poll(self):
        """Indicate if the calendar should be polled.

        If the last call to update or get_api_events was not within the minimum
        update time, then async_schedule_update_ha_state(True) is also called.
        :return: True
        :rtype: boolean
        """
        this_call = hanow()
        if (
            self._last_event_list is None
            or self._last_call is None
            or (this_call - self._last_call) > MIN_TIME_BETWEEN_UPDATES
        ):
            self._last_call = this_call
            self.async_schedule_update_ha_state(True)
        return True

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ):
        """Get all events in a specific time frame.

        :param hass: Home Assistant object
        :type hass: HomeAssistant
        :param start_date: The first starting date to consider
        :type start_date: datetime
        :param end_date: The last starting date to consider
        :type end_date: datetime
        """
        this_call = hanow()
        if (
            self._last_event_list is None
            or self._last_call is None
            or (this_call - self._last_call) > MIN_TIME_BETWEEN_UPDATES
        ):
            _LOGGER.debug(
                "%s: async_get_events called; calling internal.", self.name
            )
            self._last_call = this_call
            self._last_event_list = await self.data.async_get_events(
                hass, start_date, end_date
            )
        return self._last_event_list

    def update(self):
        """Get the current or next event."""
        self.data.update()
        event = copy.deepcopy(self.data.event)
        if event is None:
            self._event = event
            return
        [summary, offset] = extract_offset(event["summary"], OFFSET)
        event["summary"] = summary
        self._event = event
        self._attr_extra_state_attributes = {
            "offset_reached": is_offset_reached(
                get_date(event["start"]), offset
            )
        }


class ICSCalendarData:
    """Class to use the calendar ICS client object to get next event."""

    def __init__(self, device_data):
        """Set up how we are going to connect to the URL.

        :param device_data Information about the calendar
        """
        self.name = device_data[CONF_NAME]
        self._days = device_data[CONF_DAYS]
        self.include_all_day = device_data[CONF_INCLUDE_ALL_DAY]
        self.parser = ICalendarParser.get_instance(device_data[CONF_PARSER])
        self.event = None
        self._calendar_data = CalendarData(
            _LOGGER,
            self.name,
            device_data[CONF_URL],
            timedelta(minutes=device_data[CONF_DOWNLOAD_INTERVAL]),
        )

        if (
            device_data[CONF_USERNAME] != ""
            and device_data[CONF_PASSWORD] != ""
        ):
            self._calendar_data.set_user_name_password(
                device_data[CONF_USERNAME], device_data[CONF_PASSWORD]
            )

    async def async_get_events(self, hass, start_date, end_date):
        """Get all events in a specific time frame.

        :param hass: Home Assistant object
        :type hass: HomeAssistant
        :param start_date: The first starting date to consider
        :type start_date: datetime
        :param end_date: The last starting date to consider
        :type end_date: datetime
        """
        event_list = []
        if await hass.async_add_executor_job(
            self._calendar_data.download_calendar
        ):
            _LOGGER.debug("%s: Setting calendar content", self.name)
            self.parser.set_content(self._calendar_data.get())
        try:
            events = self.parser.get_event_list(
                start=start_date,
                end=end_date,
                include_all_day=self.include_all_day,
            )
            event_list = list(map(self.format_dates, events))
        except:  # pylint: disable=W0702
            _LOGGER.error(
                "async_get_events: %s: Failed to parse ICS!",
                self.name,
                exc_info=True,
            )
            event_list = []

        return event_list

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    def update(self):
        """Get the current or next event."""
        _LOGGER.debug("%s: Update was called", self.name)
        if self._calendar_data.download_calendar():
            _LOGGER.debug("%s: Setting calendar content", self.name)
            self.parser.set_content(self._calendar_data.get())
        try:
            self.event = self.parser.get_current_event(
                include_all_day=self.include_all_day,
                now=hanow(),
                days=self._days,
            )
        except:  # pylint: disable=W0702
            _LOGGER.error(
                "update: %s: Failed to parse ICS!", self.name, exc_info=True
            )
        if self.event is not None:
            _LOGGER.debug(
                "%s: got event: %s; start: %s; end: %s; all_day: %s",
                self.name,
                self.event["summary"],
                self.event["start"],
                self.event["end"],
                self.event["all_day"],
            )
            self.event["start"] = self.get_hass_date(
                self.event["start"], self.event["all_day"]
            )
            self.event["end"] = self.get_hass_date(
                self.event["end"], self.event["all_day"]
            )
            return True

        _LOGGER.debug("%s: No event found!", self.name)
        return False

    def format_dates(self, event):
        """Format the dates in the event for HA.

        :param event: The event
        :return: The event with the dates and times formatted as a string.
        """
        event["start"] = self.get_date_formatted(
            event["start"], event["all_day"]
        )
        event["end"] = self.get_date_formatted(event["end"], event["all_day"])
        return event

    def get_date_formatted(  # pylint: disable=R0201
        self, date_time: datetime, is_all_day: bool
    ) -> str:
        """Return the formatted date.

        :param date_time The datetime to be formatted
        :type date_time datetime
        :param is_all_day True if this is an all day event
        :type is_all_day bool
        :returns The datetime formatted as a date and time if is_all_day is
            false, or the datetime formatted as a date.
        :rtype str
        """
        # Note that all day events should have a time of 0, and the timezone
        # must be local.
        if is_all_day:
            return date_time.strftime("%Y-%m-%d")

        return date_time.strftime("%Y-%m-%dT%H:%M:%S.%f%z")

    def get_hass_date(self, date_time, is_all_day: bool):
        """Return the wrapped and formatted date.

        :param date_time The datetime or date
        :type date_time datetime or date
        :param is_all_day True if this is an all day event
        :type is_all_day bool
        :returns An object with the date or date and time, which indicates if
            it's a date only or a date and time.
        """
        if is_all_day:
            return {"date": self.get_date_formatted(date_time, is_all_day)}
        return {"dateTime": self.get_date_formatted(date_time, is_all_day)}
