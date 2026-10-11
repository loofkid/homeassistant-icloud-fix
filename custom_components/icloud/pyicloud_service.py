"""Home Assistant-specific pyicloud service behavior."""

import logging
from typing import Any

from pyicloud import PyiCloudService
from pyicloud.exceptions import (
    PyiCloud2FARequiredException,
    PyiCloudAuthRequiredException,
    PyiCloudException,
)

_LOGGER = logging.getLogger(__name__)


class IcloudFindMyAuthRequiredError(PyiCloudException):
    """Find My authentication failed without attempting account-wide SRP."""


class IcloudAppleAccountLockedError(PyiCloudException):
    """Apple has explicitly locked the account for security reasons."""


def is_apple_account_locked(error: BaseException) -> bool:
    """Return whether an exception chain contains Apple's account-lock code."""
    current: BaseException | None = error
    seen: set[int] = set()

    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, "response", None)
        if response is not None:
            try:
                payload = response.json()
            except (AttributeError, TypeError, ValueError):
                payload = None

            service_errors = (
                payload.get("serviceErrors") if isinstance(payload, dict) else None
            )
            if isinstance(service_errors, list) and any(
                isinstance(item, dict) and str(item.get("code")) == "-20209"
                for item in service_errors
            ):
                return True

        current = current.__cause__ or current.__context__

    return False


def _safe_apple_response_details(
    error: BaseException,
) -> tuple[int | None, str | None]:
    """Return only the HTTP status and numeric Apple service-error code."""
    current: BaseException | None = error
    seen: set[int] = set()
    status: int | None = None

    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, "response", None)
        if response is not None:
            candidate_status = getattr(response, "status_code", None)
            if isinstance(candidate_status, int) and 100 <= candidate_status <= 599:
                status = candidate_status

            try:
                payload = response.json()
            except (AttributeError, TypeError, ValueError):
                payload = None

            service_errors = (
                payload.get("serviceErrors") if isinstance(payload, dict) else None
            )
            if isinstance(service_errors, list):
                for item in service_errors:
                    code = item.get("code") if isinstance(item, dict) else None
                    if isinstance(code, (int, str)) and str(code).lstrip("-").isdigit():
                        return status, str(code)

        current = current.__cause__ or current.__context__

    return status, None


class IcloudPyiCloudService(PyiCloudService):
    """Handle Find My's forced authentication without an SRP fallback."""

    def __init__(
        self,
        *args: Any,
        suppress_srp_fallback_on_cached_hsa2: bool = False,
        **kwargs: Any,
    ) -> None:
        """Initialize pyicloud with an optional startup-only MFA guard."""
        self._suppress_srp_fallback_on_cached_hsa2 = (
            suppress_srp_fallback_on_cached_hsa2
        )
        self._last_find_my_auth_status: int | None = None
        super().__init__(*args, **kwargs)
        self._capture_find_my_auth_status()

    def _capture_find_my_auth_status(self) -> None:
        """Retain only the status code from pyicloud's Find My 421/450 retry."""
        original_post = self.session.post

        def post(*args: Any, **kwargs: Any) -> Any:
            try:
                return original_post(*args, **kwargs)
            except PyiCloudAuthRequiredException as error:
                response = getattr(error, "response", None)
                status = getattr(response, "status_code", None)
                self._last_find_my_auth_status = (
                    status if isinstance(status, int) else None
                )
                raise

        self.session.post = post

    @property
    def last_trust_session_result(self) -> bool | None:
        """Return the outcome of the most recent session-trust request."""
        return getattr(self, "_icloud_trust_session_result", None)

    def reset_trust_session_result(self) -> None:
        """Clear the trust outcome before validating a new verification code."""
        self._icloud_trust_session_result = None

    def trust_session(self) -> bool:
        """Record whether pyicloud successfully established session trust."""
        result = super().trust_session()
        self._icloud_trust_session_result = result
        return result

    def _authenticate_with_token(self, require_trust: bool = True) -> None:
        """Log safe metadata when Apple's cached-token exchange is rejected."""
        try:
            super()._authenticate_with_token(require_trust=require_trust)
        except Exception as error:  # noqa: BLE001
            cause = error
            status = None
            for _ in range(5):
                if cause is None:
                    break
                response = getattr(cause, "response", None)
                candidate = getattr(response, "status_code", None)
                if candidate is None:
                    candidate = getattr(cause, "code", None)
                if isinstance(candidate, int) and 100 <= candidate <= 599:
                    status = candidate
                    break
                if (
                    isinstance(candidate, str)
                    and candidate.isdecimal()
                    and len(candidate) <= 3
                    and 100 <= int(candidate) <= 599
                ):
                    status = int(candidate)
                    break
                cause = cause.__cause__ or cause.__context__

            _LOGGER.warning(
                "iCloud cached account-token exchange failed "
                "(error=%s, status=%s)",
                type(error).__name__,
                status if status is not None else "unknown",
            )

            if is_apple_account_locked(error):
                _LOGGER.error(
                    "Apple explicitly reported that the iCloud account is locked; "
                    "automatic authentication fallback is stopped"
                )
                raise IcloudAppleAccountLockedError(
                    "Apple has locked this account for security reasons"
                ) from error

            if (
                self._suppress_srp_fallback_on_cached_hsa2
                and self.session.data.get("session_token")
                and isinstance(error, PyiCloud2FARequiredException)
            ):
                _LOGGER.warning(
                    "Cached iCloud session requires HSA2; suppressing automatic "
                    "password/SRP fallback"
                )
                raise IcloudFindMyAuthRequiredError(
                    "The cached iCloud session requires two-factor authentication"
                ) from error

            raise

    def authenticate(
        self,
        force_refresh: bool = False,
        service: str | None = None,
        pause_2fa: bool | None = None,
    ) -> None:
        """Keep Find My-only login separate from account-wide auth."""
        # A 450 Find My response requires full account authentication; 421/500
        # are not converted to PyiCloudAuthRequiredException in pyicloud 2.7.0.
        # Its Find My manager retries 450 with an unscoped forced refresh, so
        # preserve that account-wide flow rather than substituting one-factor.
        if service == "find":
            self._authenticate_find_my()
            return

        if force_refresh and service is None:
            status = self._last_find_my_auth_status
            self._last_find_my_auth_status = None
            _LOGGER.warning(
                "Unscoped forced iCloud authentication requested "
                "(Find My status=%s)",
                status if status is not None else "unknown",
            )

        super().authenticate(
            force_refresh=force_refresh,
            service=service,
            pause_2fa=pause_2fa,
        )

    def _authenticate_find_my(self) -> None:
        """Validate the cache, then try only Apple's Find My login path."""
        has_cached_session = bool(self.session.data.get("session_token"))
        previous_data = self.data
        previously_advertised = self._find_my_supports_one_factor()
        try:
            status = self.get_auth_status()
        except Exception as error:
            if is_apple_account_locked(error):
                raise IcloudAppleAccountLockedError(
                    "Apple has locked this account for security reasons"
                ) from error
            _LOGGER.warning(
                "Could not validate cached iCloud state before Find My login "
                "(error=%s); account-wide SRP fallback suppressed",
                type(error).__name__,
            )
            raise IcloudFindMyAuthRequiredError(
                "Could not validate the cached iCloud session"
            ) from error

        # get_auth_status() clears in-memory auth data when the cache is
        # invalid. Preserve the last Apple-advertised capability only for this
        # explicit targeted login attempt; never use it to authorize SRP.
        find_can_launch_one_factor = (
            self._find_my_supports_one_factor()
            if status.get("authenticated")
            else previously_advertised
        )
        if not status.get("authenticated") and previously_advertised:
            # A failed cache probe clears self.data. Restore only the last
            # Apple app-capability response so pyicloud's service-specific
            # helper can make the targeted login attempt; its successful
            # login replaces this data before any state is used.
            self.data = previous_data

        _LOGGER.warning(
            "Find My forced authentication state "
            "(cached_session=%s, authenticated=%s, trusted=%s, "
            "requires_2fa=%s, requires_2sa=%s, find_one_factor=%s)",
            has_cached_session,
            status.get("authenticated"),
            status.get("trusted_session"),
            status.get("requires_2fa"),
            status.get("requires_2sa"),
            find_can_launch_one_factor,
        )

        if not find_can_launch_one_factor:
            _LOGGER.warning(
                "Find My one-factor login unavailable; account-wide SRP "
                "fallback suppressed"
            )
            raise IcloudFindMyAuthRequiredError(
                "Apple did not advertise Find My one-factor authentication"
            )

        _LOGGER.warning("Find My one-factor login attempt started")
        try:
            self._try_service_one_factor_login("find")
        except IcloudFindMyAuthRequiredError:
            raise
        except Exception as error:
            if is_apple_account_locked(error):
                raise IcloudAppleAccountLockedError(
                    "Apple has locked this account for security reasons"
                ) from error
            status, apple_code = _safe_apple_response_details(error)
            _LOGGER.warning(
                "Find My one-factor login raised %s "
                "(status=%s, apple_code=%s); account-wide SRP fallback suppressed",
                type(error).__name__,
                status if status is not None else "unknown",
                apple_code if apple_code is not None else "unknown",
            )
            raise IcloudFindMyAuthRequiredError(
                "Find My one-factor authentication failed"
            ) from error

        self._update_state()
        try:
            status = self.get_auth_status()
        except Exception as error:
            if is_apple_account_locked(error):
                raise IcloudAppleAccountLockedError(
                    "Apple has locked this account for security reasons"
                ) from error
            _LOGGER.warning(
                "Could not verify iCloud state after Find My one-factor login "
                "(error=%s); account-wide SRP fallback suppressed",
                type(error).__name__,
            )
            raise IcloudFindMyAuthRequiredError(
                "Could not verify the Find My authentication result"
            ) from error
        _LOGGER.warning(
            "Find My one-factor login completed "
            "(authenticated=%s, trusted=%s, requires_2fa=%s, requires_2sa=%s)",
            status.get("authenticated"),
            status.get("trusted_session"),
            status.get("requires_2fa"),
            status.get("requires_2sa"),
        )
        if not (
            status.get("authenticated")
            and status.get("trusted_session")
            and not status.get("requires_2fa")
            and not status.get("requires_2sa")
        ):
            _LOGGER.warning(
                "Find My auth is still not trusted; account-wide SRP "
                "fallback suppressed"
            )
            raise IcloudFindMyAuthRequiredError(
                "Find My authentication did not produce a trusted session"
            )

    def _find_my_supports_one_factor(self) -> bool:
        """Return Apple's advertised Find My one-factor capability."""
        apps = self.data.get("apps")
        find_app = apps.get("find") if isinstance(apps, dict) else None
        return (
            isinstance(find_app, dict)
            and find_app.get("canLaunchWithOneFactor") is True
        )

    def _try_service_one_factor_login(self, service: str | None) -> bool:
        """Prevent a failed Find My one-factor attempt from falling back to SRP."""
        if service == "find":
            succeeded = super()._try_service_one_factor_login(service)
            _LOGGER.warning(
                "Find My one-factor login %s",
                "succeeded"
                if succeeded
                else "failed; account-wide SRP fallback suppressed",
            )
            if not succeeded:
                raise IcloudFindMyAuthRequiredError(
                    "Find My one-factor authentication failed"
                )
            return succeeded

        return super()._try_service_one_factor_login(service)

    def _authenticate_with_credentials_service(self, service: str | None) -> None:
        """Log only the outcome class of a Find My service-login failure."""
        try:
            super()._authenticate_with_credentials_service(service)
        except Exception as error:
            if is_apple_account_locked(error):
                raise IcloudAppleAccountLockedError(
                    "Apple has locked this account for security reasons"
                ) from error
            if service == "find":
                status, apple_code = _safe_apple_response_details(error)
                _LOGGER.warning(
                    "Find My one-factor request raised %s "
                    "(status=%s, apple_code=%s)",
                    type(error).__name__,
                    status if status is not None else "unknown",
                    apple_code if apple_code is not None else "unknown",
                )
            raise
