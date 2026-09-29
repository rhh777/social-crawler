# Account browser desktops

The application image installs KasmVNC 1.5.0 and Openbox. Each account window
gets its own desktop and reuses the account's Chromium or AdsPower profile.

Open the browser from the console, log in, then click **Save login and close**.
The application verifies the account before saving its cookies. If verification
fails, the window stays open. Closing the window releases its account and profile
locks; other accounts can continue collecting.

The viewer uses `/browser/<session>/` on the console's port. HTTP and WebSocket
requests pass through a session-authenticated gateway. KasmVNC listens on loopback
with a random password, so no additional desktop port needs to be exposed.
A session closes after 120 seconds without a heartbeat or client activity.

Headed AdsPower collection runs also get one desktop per task. Their viewer is
read-only and follows the collection lifetime rather than the 120-second account
browser lease. Different profiles can be watched in separate console windows.

On macOS without KasmVNC, the application opens a native browser window.
Linux containers require KasmVNC. AdsPower sidecars share the app's X11 socket,
and the account window starts the profile on its desktop's display.

See [account and browser design](../../../docs/design/accounts-and-browsers.md)
for session storage and access control. Chinese input and clipboard support
also depend on the viewer's browser.
