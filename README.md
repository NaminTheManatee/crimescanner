# CrimeScanner

CrimeScanner is a Windows desktop application for monitoring Star Citizen `Game.log` crime events. It parses local game logs, enriches player information from the Roberts Space Industries (RSI) website, stores history locally, and presents the results in a dark Electron dashboard.

The app is built as an Electron + React frontend with a bundled Python backend. Users install and run it like a normal Windows application; Python and Node.js are only required for development/building, not for end users.

\---

## Features

* Monitors Star Citizen `Game.log`
* Parses crime events against you and by you
* Displays latest reports on the dashboard
* Maintains full event history
* Provides statistics for:

  * total events
  * events against you
  * your crimes
  * most killed
  * killed you most
  * most encountered organizations
* Fetches RSI player and organization metadata:

  * player profile links
  * player avatars
  * organization names
  * organization links
  * organization logos
* Caches parsed data and RSI metadata locally
* Supports selectable accent themes
* Supports minimize-to-tray behavior
* Includes installer/uninstaller support
* Supports GitHub Releases auto-update flow

\---

## How It Is Coded

CrimeScanner is split into three main layers.

### 1\. Electron Main Process

The Electron main process is located in:

```text
electron/main.js
```

It is responsible for:

* creating the desktop window
* setting the app icon
* managing the tray icon
* hiding the Python backend console window
* handling app close/minimize behavior
* exposing IPC handlers to the renderer
* launching and communicating with the Python backend
* checking GitHub Releases for app updates

The Electron main process starts the backend as a child process and communicates with it through request/response messages. The backend runs silently, so end users do not see a command prompt.

\---

### 2\. React Renderer

The renderer UI is located in:

```text
renderer/src/
```

It is built with React and Vite.

The renderer handles:

* Dashboard
* History tab
* Statistics tab
* Settings tab
* search/filter UI
* theme color selection
* rendering latest reports
* rendering RSI player/org metadata
* displaying monitoring status and countdown timers

The renderer does not directly access the filesystem. It talks to the Electron main process through the preload bridge.

\---

### 3\. Python Backend

The backend is located in:

```text
backend/crimescanner\\\_api.py
```

It handles the app’s data logic:

* reads Star Citizen `Game.log`
* reads backup logs when available
* parses crime events
* deduplicates repeated crime notifications
* ignores NPC/test-style names such as `PU\\\_Human-Test...`
* stores event history in SQLite
* loads and saves settings
* fetches RSI metadata
* caches RSI metadata locally
* preserves known metadata when new parses do not include fresh RSI results

The backend is bundled into a standalone `.exe` with PyInstaller during the build process.

\---

## How Parsing Works

The app monitors the configured Star Citizen game folder for:

```text
Game.log
```

It also checks the backup log folder when present:

```text
logbackups/
```

CrimeScanner looks for Star Citizen crime notification patterns, extracts:

* timestamp
* player name
* crime name
* whether the event was against you or against another target

It then normalizes those entries into event records and stores them locally.

The app parses once at startup, then continues scanning at a timed interval. After the initial scan, most information is cached; later scans only need to process new or changed log data.

\---

## RSI Metadata Fetching

For each parsed player, the backend builds a profile URL like:

```text
https://robertsspaceindustries.com/en/citizens/<player>
```

It then attempts to fetch:

* player avatar
* organization name
* organization URL
* organization logo

Metadata is cached locally so the same profile does not need to be fetched repeatedly.

If RSI data cannot be fetched during a later parse, the app preserves previously known metadata instead of replacing it with `Unknown`.

\---

## Local Data and Cache

CrimeScanner stores local runtime data such as:

* settings
* parsed event history
* RSI metadata cache
* image/cache data

## How to Use the App

### 1\. Install CrimeScanner

Run the installer:

```text
CrimeScanner Setup x.x.x.exe
```

Choose your preferred install location when prompted.

The installer may also ask whether to:

* create a desktop shortcut
* launch the app after installation

\---

### 2\. Set the Game Folder

Open the app and go to:

```text
Settings → Game Path Set → Change Game Folder
```

Choose the Star Citizen folder that contains:

```text
Game.log
```

Example:

```text
StarCitizen\\LIVE
```

After the folder is selected, the app parses the log and begins monitoring.

\---

### 3\. View Latest Reports

The Dashboard shows:

* total events
* events against you
* your crimes
* latest reports

The latest reports panel shows the newest entries and adapts to the available window size.

\---

### 4\. View History

The History tab shows all parsed entries.

You can search by:

* player name
* organization
* crime
* time/date

\---

### 5\. View Statistics

The Statistics tab summarizes repeated encounters and combat/crime trends.

Clicking a stat entry, such as a player or organization name, opens History and searches for that value automatically.

When you leave History, that search is cleared.

\---

### 6\. Change Appearance

Go to:

```text
Settings → Appearance
```

Select an accent color. The renderer and SVG icons update to match the selected theme.

\---

### 7\. Window Behavior

In Settings, the app includes a close-behavior option.

Depending on the setting:

* closing the window exits the program
* or closing the window hides it to the system tray

## Installer Build

The app uses:

* `electron-builder`
* NSIS installer
* PyInstaller backend bundling



The installer supports:

* choosing installation path
* desktop shortcut option
* launch after install option
* Windows uninstall entry
* app data removal on uninstall when configured

\---

## Important Notes

* End users do not need Python, Node.js, npm, or Git.
* Developers do need Python, Node.js, npm, and PyInstaller.
* Auto-update works after the app has been installed from a release installer.
* Windows SmartScreen warnings may still appear unless the app is code-signed.
* A code-signing certificate is required to reduce or remove “Unknown Publisher” warnings.

\---

## 

