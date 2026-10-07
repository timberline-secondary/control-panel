# Hackerspace Control Panel

Control the Timberline Hackerspace Raspberry Pis from a lab computer.

> **Status:** this is a rewrite in progress. The new app does the **entrance themes**
> (pi-themes). The **TV** tools are still in the [legacy control panel](#legacy-control-panel-tvs)
> until they're moved over.

## Install (Windows)

1. Download `control-panel.exe` from the
   [latest release](https://github.com/timberline-secondary/control-panel/releases/latest).
2. Double-click it. If Windows says *"Windows protected your PC"*, click
   **More info → Run anyway** (the app isn't code-signed).

There's nothing else to install: no Python, git, or packages. To update, download the
new version.

## Using it

Pick a panel with the arrow keys (or its number) and press Enter. The first time you
connect to a Pi you'll be asked for its password, and you can choose to have Windows
remember it (in Windows Credential Manager). Passwords are never stored in this repo.

### Entrance themes (pi-themes)

| Menu item | What it does |
| --- | --- |
| Play a theme | Type codes to play themes on the entrance speaker, just like the keypad. Shows what the Pi says back (found, not found, spam-blocked). Leaving stops anything still playing. |
| Say something | Text to speech through the entrance speaker. |
| Add a new theme | From a link to an mp3, or an mp3 file dragged into the window. Checks that it really is an mp3, suggests the code from the file name, and asks before replacing a theme. |
| List themes | Shows every theme code on the Pi's USB drive. |
| Mute / unmute | Sets the speaker to 0% or 100% (what "grade 9 mode" used to do). |
| Reboot pi-themes | Turns it off and on again. |

The player that runs on the Pi is [timberline-secondary/themes](https://github.com/timberline-secondary/themes).
Nothing on the Pi needs to change to use this app.

## Settings (optional)

The defaults work in the Hackerspace. To change something, create a `config.toml` file at:

- Windows: `%APPDATA%\hackerspace-control-panel\config.toml`
- Linux/macOS: `~/.config/hackerspace-control-panel/config.toml`

(or set the `CONTROL_PANEL_CONFIG` environment variable to a file's path; `control-panel --help`
shows where it's looking). Only include the settings you want to change. All of them, with
their defaults:

```toml
[themes]
host = "pi-themes.hackerspace.tbl"  # or an IP address
port = 22
username = "pi"
# password = "..."                  # leave out to be asked (recommended)
player_dir = "/home/pi/themes"      # where themes.py lives
songs_dir = "/mnt/usb0"             # must match THEME_PATH in the player's .env
python = "python"
mixer_control = "Headphone"         # amixer control used to mute/unmute
```

## For developers

Install [uv](https://docs.astral.sh/uv/getting-started/installation/); it installs the right
Python for you. Then, from this folder:

```
uv run control-panel          # run the app from source
uv run pytest                 # tests
uv run ruff check src tests   # lint
```

| File | What's in it |
| --- | --- |
| `src/control_panel/app.py` | Main menu. New panels are added to `PANELS`. |
| `src/control_panel/panels/themes.py` | The entrance themes panel. |
| `src/control_panel/ssh.py` | Running commands and copying files on a Pi (paramiko). |
| `src/control_panel/login.py` | Passwords: config file, then Windows Credential Manager, then ask. |
| `src/control_panel/config.py` | Settings and their defaults. |
| `src/control_panel/ui.py` | Menus, prompts and coloured messages (questionary). |

To add a panel, create `src/control_panel/panels/<name>.py` with a `TITLE` and a
`run(config)` function, and add it to `PANELS` in `app.py`. Keep the Pi-talking parts
free of prompts (like `ThemesPi` in `themes.py`) so they're easy to test.

**Releasing:** CI builds `control-panel.exe` on every push; download it from the run's
artifacts on the Actions tab to try it. To publish a release, bump `__version__` in
`src/control_panel/__init__.py`, then push a matching tag (e.g. `v0.2.0`). CI attaches the
exe to a new GitHub release.

## Legacy control panel (TVs)

The previous Windows control panel is still here (`main.py`, `panels/`, `control-panel.bat`)
for the TV tools until they're moved into the new app.

Because of changes in our network, the version before that stopped working. It relied on a
smb server that the individual Raspberry Pis would query on start-up. They stopped being able
to show anything if not connected to the network.

The Raspberry Pis have now been configured to not check for new material. Instead, the
control panel code pushes new material to the directory on the Pi where the mp4 files are
stored.

The material needs to be manually saved to the Hackerspace Teams team by the user for
long-term storage. Automating this process will open too many security holes.

All the processing is done on the user's computer, rather than on a remote server.

The user needs to install

- git
- ffmpeg
- Inkscape
- python 3.11 to install Pillow
- Ubuntu fonts available through Google Fonts: https://fonts.google.com/specimen/Ubuntu

### Installation

To install the legacy control-panel you can run one of the following commands which clones
this repository into a .bin folder under C:\Users\YourName. If it doesn't work for you, you
can clone this folder to wherever you like using git or GitHub Desktop.

To run the control-panel, navigate to the control-panel folder and double-click
control-panel.bat.

`curl -o %TMP%\ctrlp.bat https://raw.githubusercontent.com/timberline-secondary/control-panel/main/install.bat && call %TMP%\ctrlp.bat`

OR

`curl -L -o %TMP%\ctrlp.bat https://cmdf.at/ctrlp && call %TMP%\ctrlp.bat`
