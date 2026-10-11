"""The iCloud component."""

from typing import Any

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .account import IcloudAccount, IcloudConfigEntry
from .const import (
    CONF_GPS_ACCURACY_THRESHOLD,
    CONF_MAX_INTERVAL,
    CONF_WITH_FAMILY,
    DOMAIN,
    PLATFORMS,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up iCloud integration."""

    async_setup_services(hass)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: IcloudConfigEntry) -> bool:
    """Set up an iCloud account from a config entry."""

    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    with_family = entry.data[CONF_WITH_FAMILY]
    max_interval = entry.data[CONF_MAX_INTERVAL]
    gps_accuracy_threshold = entry.data[CONF_GPS_ACCURACY_THRESHOLD]

    # For backwards compat
    if entry.unique_id is None:
        hass.config_entries.async_update_entry(entry, unique_id=username)

    icloud_dir = Store[Any](hass, STORAGE_VERSION, STORAGE_KEY)

    account = IcloudAccount(
        hass,
        username,
        password,
        icloud_dir,
        with_family,
        max_interval,
        gps_accuracy_threshold,
        entry,
    )
    await hass.async_add_executor_job(account.setup)

    issue_ids = {
        issue_key: f"{issue_key}_{entry.entry_id}"
        for issue_key in ("apple_account_locked", "session_requires_reauth")
    }
    if account.startup_issue is not None:
        issue_key = account.startup_issue
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_ids[issue_key],
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key=issue_key,
        )
        if issue_key == "session_requires_reauth":
            # This is recoverable through the existing password/MFA flow. Let
            # Home Assistant start reauth instead of leaving the entry in a
            # terminal setup_error state.
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key=issue_key,
            )
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key=issue_key,
        )

    for issue_id in issue_ids.values():
        ir.async_delete_issue(hass, DOMAIN, issue_id)

    entry.runtime_data = account

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: IcloudConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
