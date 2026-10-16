# Home Assistant Apple iCloud 2FA Fix

Unofficial patched version of the [Home Assistant **Apple iCloud**](https://www.home-assistant.io/integrations/icloud) integration.



This custom component fixes a problem where Home Assistant asks for an Apple verification code during setup or reauthentication, but Apple does not actually send or show a usable 2FA code.
It updates the iCloud authentication flow so that Home Assistant explicitly requests an Apple two-factor authentication code before showing the verification-code form.

There are several long-running **issues** related to this problem, including:
 
- home-assistant/core#67510 - [iCloud integration causes PIN code requests on Apple devices](https://github.com/home-assistant/core/issues/67510)
- home-assistant/core#101816 - [iCloud integration continuously pops MFA approval](https://github.com/home-assistant/core/issues/101816)
- home-assistant/core#128830 - [iCloud integration stopped working due to Apple SRP-6a implementation](https://github.com/home-assistant/core/issues/128830)
- home-assistant/core#160536 - [iCloud stopped working after updating to 2026.1](https://github.com/home-assistant/core/issues/160536)
- home-assistant/core#170959 - [Apple iCloud Integration not working for months](https://github.com/home-assistant/core/issues/170959)

**Relevant upstream PR:** home-assistant/core#171863

### What This Fixes

In the original integration, users may see this kind of message after a few weeks:

```text
Authentication for your Apple Account has expired.
Your previously entered password is no longer working.
Update your password to keep using this integration.
```

After entering the regular icloud account password, Home Assistant asks for an Apple verification code. In many cases, however, no code appears on any Apple device, making reauthentication impossible without deleting and recreating the whole integration.



This is an unofficial workaround based on the Home Assistant Core `icloud` integration with the following changes:

- Uses `pyicloud==2.7.0`
- Calls `request_2fa_code()` before showing the verification-code form
- Avoids requesting a new Apple code on every failed code entry
- Validates the saved session and uses Apple's advertised Find My one-factor login for Find My forced-auth retries
- Stops after a failed Find My-specific login instead of silently falling back to account-wide SRP and another MFA cycle
- Preserves pyicloud's account-wide forced-refresh behavior for Find My HTTP 450 while avoiding cached-token HSA2 retry loops at startup
- Bounds pyicloud HTTP calls so a stalled Apple request cannot hold Home Assistant startup until its bootstrap timeout
- Routes startup authentication failures through Home Assistant's reauthentication flow instead of completing setup without an API client
- Re-checks authentication after Find My initialization, which can trigger a forced refresh
- Keeps the existing Apple iCloud integration behavior otherwise unchanged

## Installation

#### 1. Create a Backup

Before installing, create a full Home Assistant backup.

#### 2. Install the Custom Component

You can install this either via **HACS** (recommended — you get automatic update notifications) or **manually** by copying the files.

##### Option A — HACS (Custom Repository)

This repository is intentionally **not** in the default HACS store because it overrides the built-in `icloud` integration. Add it as a custom repository instead:

1. In Home Assistant, open **HACS**.
2. Open the top-right **⋮ menu → Custom repositories**.
3. Enter the repository URL `https://github.com/mdeuerlein/homeassistant-icloud-fix`, choose type **Integration**, then click **Add**.
4. Search for **Apple iCloud 2FA Fix** in HACS and click **Download**.
5. Continue with step 3 (restart) below.

> **Note:** While installed, this replaces Home Assistant's built-in Apple iCloud integration (they share the same `icloud` domain).

##### Option B — Manual (ZIP)

Download [ha-icloud-fix.zip](https://github.com/mdeuerlein/homeassistant-icloud-fix/releases/latest/download/ha-icloud-fix.zip) from the [Releases](https://github.com/mdeuerlein/homeassistant-icloud-fix/releases/) page, unpack its content and copy the `icloud` folder into your Home Assistant `custom_components` directory:

```text
/config/custom_components/icloud/
```
After installation, the path should look like this:

```text
/config/custom_components/icloud/manifest.json
/config/custom_components/icloud/config_flow.py
/config/custom_components/icloud/account.py
...
```

If the `custom_components` or the `icloud` directory does not exist yet, create it. Do not copy/overwrite this package into `icloud3`.

#### 3. Restart Home Assistant

Restart Home Assistant completely! (*A simple reload of integrations is not enough.*)

#### 4. Reauthenticate Apple iCloud

Go to `Settings -> Devices & services -> Apple iCloud`

Then reconfigure or reauthenticate the integration and you should now receive an Apple verification code on one of your trusted Apple device.


## Troubleshooting

#### Asked to sign in again after every Home Assistant restart

After a successful login, `pyicloud` stores the Apple session and cookies in
`/config/.storage/icloud/` as account-specific `.session` and `.cookiejar`
files. Those files are what allow authentication to survive a Home Assistant
restart. Keep that directory persistent and writable; do not remove or rename it
as part of normal restarts.

If Home Assistant asks you to sign in again after a restart:

1. Check that the `.session` and `.cookiejar` files still exist under
   `/config/.storage/icloud/` after the restart. File names are derived from the
   Apple Account address; do not share the file names, directory listing, files,
   or their contents.
2. If they disappeared, check that Home Assistant's `/config` (including
   `.storage`) is on the same persistent storage across restarts. For Container
   installs, verify the `/config` volume mapping.
3. If the files remain, check the Home Assistant logs for `pyicloud` and
   `custom_components.icloud` messages during startup. Redact account
   identifiers, passwords, verification codes, cookies, and tokens before
   sharing logs.

   A cached session can still expire or stop being trusted by Apple even when
   both files are present. The current `pyicloud` version distinguishes a valid
   token from a trusted browser and only reuses a cached session when Apple
   reports it as trusted. If trust has actually been revoked, a fresh
   verification may still be required; it does not necessarily mean the
   password is wrong.

When Find My requests a forced refresh, this custom integration validates the
saved session and uses Apple's Find My one-factor path when Apple's current app
data advertises it. If that targeted login is unavailable or fails, the flow
stops rather than silently falling back to account-wide SRP on that targeted
path. HTTP 450 remains pyicloud's account-wide forced-refresh path. If a cached
session needs HSA2 or the forced refresh still requires user action, startup
returns through Home Assistant's reauthentication flow. Apple HTTP calls have
bounded connect/read timeouts, and temporary network failures are reported to
Home Assistant for retry instead of blocking the integration setup task.
Sanitized logs record only authentication-state booleans and exception types,
not account credentials or session tokens.

#### No Apple Code Appears

If no Apple verification code appears even after installing this custom component, you can reset the old iCloud session cache and restart Home Assistant:

```bash
mv /config/.storage/icloud /config/.storage/icloud.backup
```

This intentionally clears the cached Apple session and will require a fresh
sign-in. Use it only as a one-time troubleshooting step, not after every restart.
Then restart Home Assistant and try to authenticate again.

#### Invalid Password

Make sure you are using the password expected by the Home Assistant Apple iCloud integration.

Depending on the current Home Assistant and Apple behavior, this may be either your normal Apple Account password during MFA setup or an app-specific password.

#### Home Assistant Still Uses the Built-In Integration

Check that the custom component path is correct:

```text
/config/custom_components/icloud/manifest.json
```

Also check the Home Assistant logs after restart. Home Assistant should load the custom integration from `custom_components`.

## How to remove/uninstall this fix

To remove this workaround:
1. Delete the folder `/config/custom_components/icloud/`
2. Restart your Home Assistant
3. The built-in Apple iCloud integration will be used again

## Disclaimer

This is not an official Home Assistant integration.

Use it at your own risk. Always create a backup before installing custom components.
