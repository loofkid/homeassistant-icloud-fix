"""iCloud account."""

from datetime import timedelta
from importlib.metadata import PackageNotFoundError, version
import logging
import operator
from typing import Any

from pyicloud.exceptions import (
    PyiCloudAPIResponseException,
    PyiCloudAuthRequiredException,
    PyiCloudFailedLoginException,
    PyiCloudNoDevicesException,
    PyiCloudServiceNotActivatedException,
    PyiCloudServiceUnavailable,
)
from pyicloud.services.findmyiphone import AppleDevice
from homeassistant.components.zone import async_active_zone
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.dispatcher import dispatcher_send
from homeassistant.helpers.event import track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify
from homeassistant.util.async_ import run_callback_threadsafe
from homeassistant.util.dt import utcnow
from homeassistant.util.location import distance

from .const import (
    ATTR_ACCOUNT_FETCH_INTERVAL,
    ATTR_BATTERY,
    ATTR_BATTERY_STATUS,
    ATTR_DEVICE_NAME,
    ATTR_DEVICE_STATUS,
    ATTR_LOW_POWER_MODE,
    ATTR_OWNER_NAME,
    DEVICE_BATTERY_LEVEL,
    DEVICE_BATTERY_STATUS,
    DEVICE_CLASS,
    DEVICE_DISPLAY_NAME,
    DEVICE_ID,
    DEVICE_LOCATION,
    DEVICE_LOCATION_HORIZONTAL_ACCURACY,
    DEVICE_LOCATION_LATITUDE,
    DEVICE_LOCATION_LONGITUDE,
    DEVICE_LOST_MODE_CAPABLE,
    DEVICE_LOW_POWER_MODE,
    DEVICE_NAME,
    DEVICE_PERSON_ID,
    DEVICE_RAW_DEVICE_MODEL,
    DEVICE_STATUS,
    DEVICE_STATUS_CODES,
    DEVICE_STATUS_SET,
    DOMAIN,
)
from .pyicloud_service import (
    IcloudAppleAccountLockedError,
    IcloudFindMyAuthRequiredError,
    IcloudPyiCloudService,
    is_apple_account_locked,
    is_transport_error,
)

_LOGGER = logging.getLogger(__name__)

try:
    _PYICLOUD_VERSION = version("pyicloud")
except PackageNotFoundError:
    _PYICLOUD_VERSION = "unknown"

type IcloudConfigEntry = ConfigEntry[IcloudAccount]


class IcloudAccount:
    """Representation of an iCloud account."""

    def __init__(
        self,
        hass: HomeAssistant,
        username: str,
        password: str,
        icloud_dir: Store,
        with_family: bool,
        max_interval: int,
        gps_accuracy_threshold: int,
        config_entry: IcloudConfigEntry,
    ) -> None:
        """Initialize an iCloud account."""
        self.hass = hass
        self._username = username
        self._password = password
        self._with_family = with_family
        self._fetch_interval: float = max_interval
        self._max_interval = max_interval
        self._gps_accuracy_threshold = gps_accuracy_threshold

        self._icloud_dir = icloud_dir

        self.api: IcloudPyiCloudService | None = None
        self._owner_fullname: str | None = None
        self._family_members_fullname: dict[str, str] = {}
        self._devices: dict[str, IcloudDevice] = {}
        self._retried_fetch = False
        self._config_entry = config_entry
        self.startup_issue: str | None = None

        self.listeners: list[CALLBACK_TYPE] = []

    def setup(self) -> None:
        """Set up an iCloud account."""
        try:
            self.api = IcloudPyiCloudService(
                self._username,
                self._password,
                self._icloud_dir.path,
                with_family=self._with_family,
                # Startup must not silently switch from a rejected cached
                # session to an account-wide password/SRP login. Let the user
                # explicitly complete reauthentication instead.
                suppress_srp_fallback_on_cached_hsa2=True,
                # Auto-accept updated Apple terms of service. Newer pyicloud
                # versions otherwise raise PyiCloudAcceptTermsException on login.
                accept_terms=True,
            )

        except IcloudAppleAccountLockedError:
            self.api = None
            self.startup_issue = "apple_account_locked"
            _LOGGER.error(
                "Apple reported that this iCloud account is locked; "
                "automatic authentication will not be retried"
            )
            return
        except (IcloudFindMyAuthRequiredError, PyiCloudAuthRequiredException) as error:
            self.api = None
            if is_transport_error(error):
                _LOGGER.warning(
                    "Apple iCloud connection failed during startup (%s); "
                    "Home Assistant will retry",
                    type(error).__name__,
                )
                raise ConfigEntryNotReady("Unable to contact Apple iCloud") from error
            self.startup_issue = "session_requires_reauth"
            _LOGGER.warning(
                "Apple requires additional authentication for the cached "
                "iCloud session; automatic password fallback was suppressed"
            )
            return
        except PyiCloudServiceNotActivatedException as error:
            _LOGGER.error("No iCloud device found")
            raise ConfigEntryNotReady from error
        except PyiCloudServiceUnavailable as error:
            _LOGGER.warning("Apple iCloud service is temporarily unavailable")
            raise ConfigEntryNotReady from error
        except PyiCloudAPIResponseException as error:
            if is_apple_account_locked(error):
                self.api = None
                self.startup_issue = "apple_account_locked"
                _LOGGER.error(
                    "Apple reported that this iCloud account is locked; "
                    "automatic authentication will not be retried"
                )
                return
            if is_transport_error(error):
                _LOGGER.warning(
                    "Apple iCloud connection failed during startup (%s); "
                    "Home Assistant will retry",
                    type(error).__name__,
                )
                raise ConfigEntryNotReady("Unable to contact Apple iCloud") from error
            raise
        except PyiCloudFailedLoginException as error:
            self.api = None
            if is_apple_account_locked(error):
                self.startup_issue = "apple_account_locked"
                _LOGGER.error(
                    "Apple reported that this iCloud account is locked; "
                    "automatic authentication will not be retried"
                )
                return
            if is_transport_error(error):
                _LOGGER.warning(
                    "Apple iCloud connection failed during startup (%s); "
                    "Home Assistant will retry",
                    type(error).__name__,
                )
                raise ConfigEntryNotReady("Unable to contact Apple iCloud") from error
            # A rejected password needs a user-driven reauthentication; signal
            # it to async_setup_entry instead of scheduling a flow from this
            # executor thread and continuing setup with no API client.
            _LOGGER.warning(
                "iCloud authentication failed (%s); user-driven "
                "reauthentication is required",
                type(error).__name__,
            )
            self.startup_issue = "session_requires_reauth"
            return

        _LOGGER.info(
            "iCloud startup session state (pyicloud=%s): trusted=%s, "
            "requires_2fa=%s, requires_2sa=%s",
            _PYICLOUD_VERSION,
            self.api.is_trusted_session,
            self.api.requires_2fa,
            self.api.requires_2sa,
        )

        if self.api.requires_2fa:
            # Apple did not accept this session as trusted. This is distinct
            # from a rejected password, even though both paths need reauth.
            _LOGGER.warning(
                (
                    "Apple is requiring two-factor authentication for the "
                    "iCloud session. A saved session can still be expired or "
                    "untrusted; this does not necessarily mean the password "
                    "is wrong. Reauthenticate the Apple iCloud integration. "
                    "(pyicloud=%s, trusted=%s, requires_2fa=%s, requires_2sa=%s)."
                ),
                _PYICLOUD_VERSION,
                self.api.is_trusted_session,
                self.api.requires_2fa,
                self.api.requires_2sa,
            )
            self.api = None
            self.startup_issue = "session_requires_reauth"
            return

        auth_state_before_device_fetch = (
            self.api.is_trusted_session,
            self.api.requires_2fa,
            self.api.requires_2sa,
        )

        try:
            # Gets device owners infos
            user_info = self.api.devices.user_info
        except IcloudAppleAccountLockedError:
            self.api = None
            self.startup_issue = "apple_account_locked"
            _LOGGER.error(
                "Apple reported that this iCloud account is locked; "
                "automatic authentication will not be retried"
            )
            return
        except (IcloudFindMyAuthRequiredError, PyiCloudAuthRequiredException) as err:
            if is_transport_error(err):
                _LOGGER.warning(
                    "Apple iCloud connection failed while loading devices (%s); "
                    "Home Assistant will retry",
                    type(err).__name__,
                )
                raise ConfigEntryNotReady("Unable to contact Apple iCloud") from err
            _LOGGER.warning(
                "Find My authentication requires a user-driven "
                "reauthentication (%s); automatic fallback was suppressed",
                type(err).__name__,
            )
            self.api = None
            self.startup_issue = "session_requires_reauth"
            return
        except (
            PyiCloudServiceNotActivatedException,
            PyiCloudNoDevicesException,
            PyiCloudServiceUnavailable,
        ) as err:
            _LOGGER.error("No iCloud device found")
            raise ConfigEntryNotReady from err
        except PyiCloudAPIResponseException as err:
            if is_apple_account_locked(err):
                self.api = None
                self.startup_issue = "apple_account_locked"
                _LOGGER.error(
                    "Apple reported that this iCloud account is locked; "
                    "automatic authentication will not be retried"
                )
                return
            if is_transport_error(err):
                _LOGGER.warning(
                    "Apple iCloud connection failed while loading devices (%s); "
                    "Home Assistant will retry",
                    type(err).__name__,
                )
                raise ConfigEntryNotReady("Unable to contact Apple iCloud") from err
            raise
        except PyiCloudFailedLoginException as err:
            self.api = None
            if is_apple_account_locked(err):
                self.startup_issue = "apple_account_locked"
                _LOGGER.error(
                    "Apple reported that this iCloud account is locked; "
                    "automatic authentication will not be retried"
                )
            elif is_transport_error(err):
                _LOGGER.warning(
                    "Apple iCloud connection failed while loading devices (%s); "
                    "Home Assistant will retry",
                    type(err).__name__,
                )
                raise ConfigEntryNotReady("Unable to contact Apple iCloud") from err
            else:
                self.startup_issue = "session_requires_reauth"
                _LOGGER.warning(
                    "Find My login failed after the cached session check "
                    "(error=%s); user-driven reauthentication is required",
                    type(err).__name__,
                )
            return
        if user_info is None:
            raise ConfigEntryNotReady("No user info found in iCloud devices response")

        if self.api.requires_2fa:
            _LOGGER.warning(
                "Apple requires two-factor authentication after initializing "
                "iCloud Find My devices (pyicloud=%s, before trusted=%s/2fa=%s/2sa=%s, "
                "after trusted=%s/2fa=%s/2sa=%s); starting reauthentication",
                _PYICLOUD_VERSION,
                *auth_state_before_device_fetch,
                self.api.is_trusted_session,
                self.api.requires_2fa,
                self.api.requires_2sa,
            )
            self.api = None
            self.startup_issue = "session_requires_reauth"
            return

        self._owner_fullname = (
            f"{user_info.get('firstName')} {user_info.get('lastName')}"
        )

        self._family_members_fullname = {}
        if user_info.get("membersInfo") is not None:
            for prs_id, member in user_info.get("membersInfo").items():
                self._family_members_fullname[prs_id] = (
                    f"{member['firstName']} {member['lastName']}"
                )

        self._devices = {}
        self.update_devices()

    def update_devices(self) -> None:
        """Update iCloud devices."""
        if self.api is None:
            return
        _LOGGER.debug("Updating devices")

        if self.api.requires_2fa:
            _LOGGER.warning(
                "Apple requires two-factor authentication while refreshing "
                "iCloud devices (pyicloud=%s, trusted=%s, requires_2fa=%s, "
                "requires_2sa=%s); starting reauthentication",
                _PYICLOUD_VERSION,
                self.api.is_trusted_session,
                self.api.requires_2fa,
                self.api.requires_2sa,
            )
            self._require_reauth()
            return

        api_devices = {}
        try:
            api_devices = list(self.api.devices)
        except (IcloudFindMyAuthRequiredError, PyiCloudAuthRequiredException) as err:
            _LOGGER.warning(
                "Apple Find My authentication is required while refreshing "
                "devices (%s); starting reauthentication",
                type(err).__name__,
            )
            self._require_reauth()
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Unknown iCloud error (error=%s)", type(err).__name__)
            self._fetch_interval = 2
            dispatcher_send(self.hass, self.signal_device_update)
            self._schedule_next_fetch()
            return

        # Gets devices infos
        new_device = False
        for device in api_devices:
            status = device.status(DEVICE_STATUS_SET)
            device_id = status[DEVICE_ID]
            device_name = status[DEVICE_NAME]

            if (
                status[DEVICE_BATTERY_STATUS] == "Unknown"
                or status.get(DEVICE_BATTERY_LEVEL) is None
            ):
                continue

            if self._devices.get(device_id) is not None:
                # Seen device -> updating
                _LOGGER.debug("Updating iCloud device: %s", device_name)
                self._devices[device_id].update(status)
            else:
                # New device, should be unique
                _LOGGER.debug(
                    "Adding iCloud device: %s [model: %s]",
                    device_name,
                    status[DEVICE_RAW_DEVICE_MODEL],
                )
                self._devices[device_id] = IcloudDevice(self, device, status)
                self._devices[device_id].update(status)
                new_device = True

        if (
            api_devices
            and DEVICE_STATUS_CODES.get(api_devices[0][DEVICE_STATUS]) == "pending"
            and not self._retried_fetch
        ):
            _LOGGER.debug("Pending devices, trying again in 15s")
            self._fetch_interval = 0.25
            self._retried_fetch = True
        else:
            self._fetch_interval = self._determine_interval()
            self._retried_fetch = False

        dispatcher_send(self.hass, self.signal_device_update)
        if new_device:
            dispatcher_send(self.hass, self.signal_device_new)

        self._schedule_next_fetch()

    def _require_reauth(self):
        """Require the user to log in again."""
        self.hass.add_job(self._config_entry.async_start_reauth, self.hass)

    def _determine_interval(self) -> int:
        """Calculate new interval between two API fetch (in minutes)."""
        intervals = {"default": self._max_interval}
        for device in self._devices.values():
            # Max interval if no location
            if device.location is None:
                continue

            current_zone = run_callback_threadsafe(
                self.hass.loop,
                async_active_zone,
                self.hass,
                device.location[DEVICE_LOCATION_LATITUDE],
                device.location[DEVICE_LOCATION_LONGITUDE],
                device.location[DEVICE_LOCATION_HORIZONTAL_ACCURACY],
            ).result()

            # Max interval if in zone
            if current_zone is not None:
                continue

            zones = (
                self.hass.states.get(entity_id)
                for entity_id in sorted(self.hass.states.entity_ids("zone"))
            )

            distances = []
            for zone_state in zones:
                if zone_state is None:
                    continue
                zone_state_lat = zone_state.attributes[DEVICE_LOCATION_LATITUDE]
                zone_state_long = zone_state.attributes[DEVICE_LOCATION_LONGITUDE]
                zone_distance = distance(
                    device.location[DEVICE_LOCATION_LATITUDE],
                    device.location[DEVICE_LOCATION_LONGITUDE],
                    zone_state_lat,
                    zone_state_long,
                )
                if zone_distance is not None:
                    distances.append(round(zone_distance / 1000, 1))

            # Max interval if no zone
            if not distances:
                continue
            mindistance = min(distances)

            # Calculate out how long it would take for the device to drive
            # to the nearest zone at 120 km/h:
            interval = round(mindistance / 2)

            # Never poll more than once per minute
            interval = max(interval, 1)

            if interval > 180:
                # Three hour drive?
                # This is far enough that they might be flying
                interval = self._max_interval

            if (
                device.battery_level is not None
                and device.battery_level <= 33
                and mindistance > 3
            ):
                # Low battery - let's check half as often
                interval = interval * 2

            intervals[device.name] = interval

        return max(
            int(min(intervals.items(), key=operator.itemgetter(1))[1]),
            self._max_interval,
        )

    def _schedule_next_fetch(self) -> None:
        if not self._config_entry.pref_disable_polling:
            track_point_in_utc_time(
                self.hass,
                self.keep_alive,
                utcnow() + timedelta(minutes=self._fetch_interval),
            )

    def keep_alive(self, now=None) -> None:
        """Keep the API alive."""
        if self.api is None:
            self.setup()

        if self.api is None:
            return

        self.api.authenticate()
        self.update_devices()

    def get_devices_with_name(self, name: str) -> list[Any]:
        """Get devices by name."""
        name_slug = slugify(name.replace(" ", "", 99))
        result = [
            device
            for device in self.devices.values()
            if slugify(device.name.replace(" ", "", 99)) == name_slug
        ]
        if not result:
            raise ValueError(f"No device with name {name}")
        return result

    @property
    def username(self) -> str:
        """Return the account username."""
        return self._username

    @property
    def owner_fullname(self) -> str | None:
        """Return the account owner fullname."""
        return self._owner_fullname

    @property
    def family_members_fullname(self) -> dict[str, str]:
        """Return the account family members fullname."""
        return self._family_members_fullname

    @property
    def fetch_interval(self) -> float:
        """Return the account fetch interval."""
        return self._fetch_interval

    @property
    def devices(self) -> dict[str, Any]:
        """Return the account devices."""
        return self._devices

    @property
    def signal_device_new(self) -> str:
        """Event specific per Freebox entry to signal new device."""
        return f"{DOMAIN}-{self._username}-device-new"

    @property
    def signal_device_update(self) -> str:
        """Event specific per Freebox entry to signal updates in devices."""
        return f"{DOMAIN}-{self._username}-device-update"


class IcloudDevice:
    """Representation of a iCloud device."""

    _attr_attribution = "Data provided by Apple iCloud"

    def __init__(self, account: IcloudAccount, device: AppleDevice, status) -> None:
        """Initialize the iCloud device."""
        self._account = account

        self._device = device
        self._status = status

        self._name = self._status[DEVICE_NAME]
        self._device_id = self._status[DEVICE_ID]
        self._device_class = self._status[DEVICE_CLASS]
        self._device_model = self._status[DEVICE_DISPLAY_NAME]

        self._battery_level: int | None = None
        self._battery_status = None
        self._location = None

        self._attrs = {
            ATTR_ACCOUNT_FETCH_INTERVAL: self._account.fetch_interval,
            ATTR_DEVICE_NAME: self._device_model,
            ATTR_DEVICE_STATUS: None,
        }
        if self._status[DEVICE_PERSON_ID]:
            self._attrs[ATTR_OWNER_NAME] = account.family_members_fullname[
                self._status[DEVICE_PERSON_ID]
            ]
        elif account.owner_fullname is not None:
            self._attrs[ATTR_OWNER_NAME] = account.owner_fullname

    def update(self, status) -> None:
        """Update the iCloud device."""
        self._status = status

        self._status[ATTR_ACCOUNT_FETCH_INTERVAL] = self._account.fetch_interval

        device_status = DEVICE_STATUS_CODES.get(self._status[DEVICE_STATUS], "error")
        self._attrs[ATTR_DEVICE_STATUS] = device_status

        self._battery_status = self._status[DEVICE_BATTERY_STATUS]
        self._attrs[ATTR_BATTERY_STATUS] = self._battery_status
        device_battery_level = self._status.get(DEVICE_BATTERY_LEVEL, 0)
        if self._battery_status != "Unknown" and device_battery_level is not None:
            self._battery_level = int(device_battery_level * 100)
            self._attrs[ATTR_BATTERY] = self._battery_level
            self._attrs[ATTR_LOW_POWER_MODE] = self._status[DEVICE_LOW_POWER_MODE]

            if (
                self._status[DEVICE_LOCATION]
                and self._status[DEVICE_LOCATION][DEVICE_LOCATION_LATITUDE]
            ):
                location = self._status[DEVICE_LOCATION]
                if self._location is None:
                    dispatcher_send(self._account.hass, self._account.signal_device_new)
                self._location = location

    def play_sound(self) -> None:
        """Play sound on the device."""
        if self._account.api is None:
            return

        self._account.api.authenticate()
        _LOGGER.debug("Playing sound for %s", self.name)
        self.device.play_sound()

    def display_message(self, message: str, sound: bool = False) -> None:
        """Display a message on the device."""
        if self._account.api is None:
            return

        self._account.api.authenticate()
        _LOGGER.debug("Displaying message for %s", self.name)
        self.device.display_message("Subject not working", message, sound)

    def lost_device(self, number: str, message: str) -> None:
        """Make the device in lost state."""
        if self._account.api is None:
            return

        self._account.api.authenticate()
        if self._status[DEVICE_LOST_MODE_CAPABLE]:
            _LOGGER.debug("Make device lost for %s", self.name)
            self.device.lost_device(number, message, None)
        else:
            _LOGGER.error("Cannot make device lost for %s", self.name)

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return self._device_id

    @property
    def name(self) -> str:
        """Return the Apple device name."""
        return self._name

    @property
    def device(self) -> AppleDevice:
        """Return the Apple device."""
        return self._device

    @property
    def device_class(self) -> str:
        """Return the Apple device class."""
        return self._device_class

    @property
    def device_model(self) -> str:
        """Return the Apple device model."""
        return self._device_model

    @property
    def battery_level(self) -> int | None:
        """Return the Apple device battery level."""
        return self._battery_level

    @property
    def battery_status(self) -> str | None:
        """Return the Apple device battery status."""
        return self._battery_status

    @property
    def location(self) -> dict[str, Any] | None:
        """Return the Apple device location."""
        return self._location

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the attributes."""
        return self._attrs
