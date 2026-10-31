# Zoom Language Launcher

A local launcher for multilingual conference setups. Enter a Zoom meeting link and passcode, choose your languages, and send a separate Zoom join request for each selected language—with an editable participant name for each client.

The launcher provides a browser-based interface backed by a small Python server running on your own computer. It includes nine default languages, saved sessions, and experimental controls for microphone channels and Zoom window actions.

> **Project status:** An experimental conference utility. Opening clients does not confirm that they have joined, and Zoom may reuse or close instances. Automatic audio routing, self-pinning, and floating mini-window behavior need verification with your installed Zoom version before use at an event. Windows multi-client behavior is particularly version-dependent.

## Features

- Launch any selection of languages, with Select all and Unselect all controls.
- Add or remove languages and edit their participant names.
- Save named sessions containing meeting details and per-language settings.
- Load recent sessions and import or export session history.
- Select an audio input and a numbered input channel for each language.
- Prefer **Loopback Audio** automatically when that device exists.
- On macOS, attempt to apply and read back each client’s microphone channel settings.
- Optionally pin each client’s own profile and switch to a floating mini window. Both options default to enabled and can be changed before launch.
- Use a local web interface without a separate frontend build or Python package dependencies.

This is an independent utility and is not affiliated with Zoom or Rogue Amoeba. Zoom Workplace and Loopback are separate applications and are not included.

## Requirements

- **macOS:** the primary development platform. The packaged Apple Silicon build targets macOS 14 or later.
- **Windows:** experimental support; installer configuration targets Windows 10/11, 64-bit.
- Zoom Workplace installed separately.
- Python 3 for running from source. Development tests have run with Python 3.14; compatibility with older Python versions has not been established.
- A web browser and an internet connection for joining Zoom meetings.
- Optional: Loopback Audio and a configured multichannel virtual input.

Linux is not currently supported.

## Start from source

Keep these files together:

```text
Zoom Language Launcher.command
zoom_language_launcher.py
zoom_audio_routing.py
zoom_window_actions.py
zoom_mac_windows.py
```

### macOS

Double-click **Zoom Language Launcher.command**. The command starts Python directly and opens the local interface in your default browser.

Alternatively, open Terminal in the project folder and run:

```sh
python3 zoom_language_launcher.py
```

If the command file is not executable:

```sh
chmod +x "Zoom Language Launcher.command"
```

The command file does not require the bundled `.app`. Keep its Terminal window open while using the launcher.

### Windows

Open a terminal in the project folder and run:

```powershell
py -3 zoom_language_launcher.py
```

Windows uses Zoom’s experimental `/secondary` launch switch. Test with two languages first: multiple launch requests do not guarantee multiple independent participants. Audio device/channel choices are saved, but automatic channel routing is currently macOS-only.

### If Zoom is installed elsewhere

Set `ZOOM_APP` to the application path before starting the launcher:

```sh
# macOS example
export ZOOM_APP="/Applications/zoom.us.app"
python3 zoom_language_launcher.py
```

```powershell
# Windows example: replace this with your actual Zoom.exe path
$env:ZOOM_APP = 'C:\path\to\Zoom.exe'
py -3 zoom_language_launcher.py
```

## Prepare Zoom

Configure Zoom’s join preferences on each computer before using the launcher:

- Disable the video preview before joining.
- Enable camera off when joining.
- Enable microphone muted when joining.
- Enable automatic connection to computer audio if required by your setup.

The launcher does not automatically enforce these preferences on a new computer. Check mute and camera status after joining. Audio channel selection does not unmute a microphone.

Waiting rooms, authentication, registration, host admission, and other meeting policies may require manual action.

## Use the launcher

1. Enter a **session name**, a Zoom meeting link or meeting ID, and the passcode if required.
2. Select the languages to launch. Edit participant names as needed.
3. Choose the audio input and input channel for each language.
4. Review **Pin my profile** and **Mini window** options. Disable them if you only want meeting launch requests.
5. Save the session, or use **Save & start** to save and launch.
6. Read the result messages, then confirm that every intended participant has actually joined with the correct name.
7. Choose the interpretation language inside each Zoom client as needed.

Saving an existing session name updates that session; matching is case-insensitive. Language names and participant names can be edited independently.

**Participant names do not grant interpreter privileges.** The meeting host must assign interpreter roles. The launcher does not automatically select Zoom interpretation channels.

Use **Quit launcher** to stop the local server. Zoom clients stay open. Closing the browser tab alone does not stop the server; a source run can also be stopped with Control-C in its terminal.

## Default languages and microphone channels

Channels are numbered starting at **1**. These defaults describe channels feeding the Zoom **microphones**, not speaker outputs or automatically selected interpretation languages.

| Language | Participant name | Input channel |
| --- | --- | ---: |
| Russian | `*R U Russian` | 17 |
| Chinese | `*Z N Chinese` | 18 |
| Dutch | `*N L Dutch` | 19 |
| French | `*F R French` | 20 |
| Spanish | `*E S Spanish` | 21 |
| Polish | `*P L polski` | 22 |
| Portuguese | `*P R Portuguese` | 23 |
| Romanian | `*R O Romanian` | 24 |
| German | `*D E German` | 25 |

The virtual input must expose the selected channel, and your Loopback configuration must feed the intended signal into it. Selecting channel 18 does not create that channel or configure Loopback’s internal routing.

### Experimental macOS audio automation

The launcher attempts to identify each newly opened Zoom process, select its microphone device and specific input channel through macOS Accessibility controls, then read the selection back. Zoom clients can share settings, so process separation alone does not guarantee independent audio behavior.

The two-client audio test opens temporary clients without joining a meeting and checks settings for channels 17 and 18. A successful settings read-back is **not an end-to-end transmitted-audio test**. Check real signals in each meeting client before relying on the routing.

Audio setup failures are reported without intentionally cancelling the remaining meeting launch requests.

### Experimental pinning and mini windows

Pinning targets the client’s own profile. Minimization is attempted independently, including when pinning fails. The launcher reports a verified floating mini-window state separately from an ordinary minimized window.

These actions depend on the controls Zoom exposes, a joined meeting, and sufficient macOS permissions. They are not guaranteed. Automatic placement into a desktop grid is not implemented.

## macOS permissions

Audio and window automation require **Accessibility** permission under:

**System Settings → Privacy & Security → Accessibility**

Grant access to the application actually running the launcher. A direct Python run and a packaged application have different permission identities; granting access to one may not authorize the other. Follow the identity shown by macOS, then quit and restart the launcher after changing permission.

The Windows window-action implementation uses PowerShell UI Automation and remains unverified on a Windows computer.

## Saved data and privacy

| Run method | Default data folder |
| --- | --- |
| macOS source / `.command` | Beside `zoom_language_launcher.py` |
| Packaged macOS application | `~/Documents/Zoom Language Launcher` |
| Windows | The current user’s Documents folder, under `Zoom Language Launcher` |

The launcher writes:

- `zoom_sessions.json`: named sessions, meeting links, passcodes, and selected settings.
- `zoom_languages.json`: editable language catalogue and defaults.
- `window_defaults_v2_4_2.json`: a one-time migration marker for default window options.

Set `ZOOM_LAUNCHER_DATA_DIR` to use a different data folder. Import/export can transfer sessions between computers or run methods.

**Saved sessions and exports contain unencrypted meeting passcodes.** Treat them as private. Meeting links may also contain embedded passcodes. Keep these files out of Git, issue reports, and screenshots. On macOS, the launcher writes its JSON files with restrictive file permissions, but this is not encryption.

The server binds to `127.0.0.1` on an available port and uses a random token for access. The interface is local, not a hosted website. Join requests are passed to the installed Zoom application. Do not expose the local server through a public tunnel.

## Troubleshooting

| Problem | What to check |
| --- | --- |
| Start command does not open the interface | Run `python3 zoom_language_launcher.py` in Terminal and read the error. Keep all source modules together. |
| Only one Zoom client appears | Zoom may be reusing instances. Test two languages first; Windows secondary-instance behavior is experimental. |
| Clients open but do not join | Check the meeting link/passcode, admission requirements, and Zoom installation. A launch-success message only confirms that requests were sent. |
| Participant name is wrong | Check the saved per-language name and whether Zoom reused an existing connection. Verify the displayed name in the meeting. |
| All clients receive the same microphone signal | Check Loopback routing, per-client input selections, and settings read-back. Disable automatic routing and configure manually if independent selections do not persist. |
| Pinning or mini windows do nothing | Check Accessibility permission for the actual launcher process and confirm the client has joined. Read the per-language Window actions result. |
| Data seems to be missing | Source and packaged runs use different default folders. Check the folder displayed in the interface, then import the intended session history. |

When reporting a problem, include the OS, Zoom version, launcher version, run method, and result message. Remove meeting IDs, passcodes, tokenized local URLs, and personal details first.

## Development and tests

The application uses Python’s standard library. No `pip install` step is needed to run the source or tests.

```sh
python3 -m unittest discover -s packaging -p 'test_*.py'
```

The test suite covers session persistence, imports, validation, access checks, platform launch-command construction, simulated audio routing, and independent pin/minimize handling. These automated checks do not replace live Zoom meeting tests or verify transmitted audio.

### Source layout

```text
zoom_language_launcher.py       Local HTTP server, embedded UI, sessions, launch flow
zoom_audio_routing.py           macOS Accessibility wrapper and microphone routing
zoom_mac_windows.py             Process-scoped macOS pin/minimize actions
zoom_window_actions.py          Platform dispatch and Windows window automation
Zoom Language Launcher.command Direct Python entry point for macOS
packaging/                     Tests, PyInstaller spec, installer definitions
```

Packaging uses PyInstaller for the macOS app and NSIS with a bundled Python runtime for Windows. Packaging tools are separate development dependencies. The existing installer definitions rely on locally prepared build inputs and are not a complete, reproducible cross-platform build pipeline. Source and installer versions may differ; check the specific artifact rather than assuming a package contains the latest source.

Locally built installers are not publisher-signed or Apple-notarized. Installer availability does not establish Windows runtime compatibility. Distribute binaries through GitHub Releases rather than committing app bundles and build directories into the source repository.

## Before publishing to GitHub

This project folder also contains local data, generated installers, and unrelated conference logs. The included `.gitignore` excludes those from a new repository; it cannot remove files that were already tracked.

1. Review the files you intend to publish and run the tests.
2. Confirm saved sessions, passcodes, logs, app bundles, and build outputs are excluded. Inspect the staged diff before committing.
3. Choose a license and add a `LICENSE` file before presenting the project as open source. No license is supplied here.
4. Create your GitHub repository, then initialize and push the reviewed source. This README does not assume a repository URL.

Contributions should describe the tested OS and Zoom version, preserve private data, and distinguish simulated test results from actual meeting and audio verification.
# av-zoom-language-launcher
