# Hackerspace Control Panel

Control the Timberline Hackerspace Raspberry Pis from a lab computer.

> **Status:** this is a rewrite of the old control panel. It does the **entrance themes**
> (pi-themes), the **TVs** (art shrines) and **grade 9 mode**.

## Install (Windows)

1. Download `control-panel.exe` from the
   [latest release](https://github.com/timberline-secondary/control-panel/releases/latest).
2. Double-click it. If Windows says *"Windows protected your PC"*, click
   **More info → Run anyway** (the app isn't code-signed).

There's nothing else to install: no Python, git, or packages.

**Updates:** when a new version is out, the app offers to update itself when it starts. Say
yes and it downloads the new version, swaps it in and restarts. (You can also run
`control-panel --update` from a terminal.)

## Using it

Pick a panel with the arrow keys (or its number) and press Enter. The first time you
connect to a Pi you'll be asked for its password. If you're on your own Windows login (not a
shared one), you can have Windows remember it in Credential Manager.

The app also remembers each Pi's identity the first time it connects. If that ever changes,
it warns you before sending the password, so another computer can't pretend to be a Pi to
collect it. That's expected after a Pi is re-installed; you'll be asked whether to trust it.

### Entrance themes (pi-themes)

| Menu item | What it does |
| --- | --- |
| Play a theme | Type codes to play themes on the entrance speaker, just like the keypad. Shows what the Pi says back (found, not found, spam-blocked). It runs its own copy of the player, so spam-block and admin (`*`) codes here don't affect the keypad. Leaving stops anything still playing. |
| Say something | Text to speech through the entrance speaker. |
| Add a new theme | From a link to an mp3, or an mp3 file dragged into the window. Checks that it really is an mp3, suggests the code from the file name. If the code is taken, you can replace that theme, pick another code, or cancel. |
| List themes | Shows every theme code on the Pi's USB drive, in dictionary order (05111955 comes before 15111955 and 4261992). Then type part of a code to see only the codes that contain it. |
| Mute / unmute | Sets the speaker to 0% or 100%. (Grade 9 mode does this and turns TVs 1-3 off in one go.) |
| Reboot pi-themes | Turns it off and on again. |

The player that runs on the Pi is [timberline-secondary/themes](https://github.com/timberline-secondary/themes).
Nothing on the Pi needs to change to use this app.

### TVs (art shrines)

The four TV Pis (`pi-tv1` to `pi-tv4`) run Raspberry Slideshow, which plays every video in
`/home/pi/rs_media` in alphabetical order, over and over. Each student's **shrine** is a set of
videos made on your computer:

- `firstname.lastname.a.mp4`: a title card ("The Digital Art of ... Grad 2027"), then each of
  their pictures for 8 seconds, crossfading from one to the next.
- `firstname.lastname.z.<name>.mp4`: each of their videos and animated GIFs, converted so the TVs
  can play them (1920x1080, no sound). Short GIFs are looped to at least 5 seconds.

| TV | What's on it |
| --- | --- |
| TV 1 | Original art, last names A-L |
| TV 2 | Original art, last names M-Z |
| TV 3 | Demos, tutorials and Skills Canada |
| TV 4 | The hallway (usually just each shrine's title slideshow) |

| Menu item | What it does |
| --- | --- |
| Make a shrine | For a student (username, name, grad year, subject) or a collection (e.g. Skills Canada, with or without a title card). Then drag in their folder or files, or paste links to pictures, videos or `.zip` files (a OneDrive/Teams folder downloads as a `.zip`). Pictures are shown in the order of their names, and subfolders are included. The student's own files are never moved or changed. The videos are saved in `Documents\Hackerspace shrines\<name>`. Making it again replaces it, e.g. after adding a new piece. |
| Put a shrine on the TVs | Suggests the TV from the student's last name, plus the hallway. Shows what that student already has on each TV and asks whether to replace it. Checks there's room, copies the videos, then restarts the slideshow so it plays them. |
| See, copy or delete the videos on a TV | List and search a TV's videos, copy some to this computer, or delete some. |
| Turn TVs on or off | Some or all of them, at the same time (using the TV's HDMI-CEC remote control commands). |
| Restart a TV's slideshow, or reboot it | Restarting the slideshow is quick and picks up new or deleted videos. |

The first time you make a shrine, the app downloads **ffmpeg** (a free video tool, about 31 MB)
and keeps it in `%LOCALAPPDATA%\hackerspace-control-panel`. It isn't built into the app because
that would make every start slower. Making a shrine takes a minute or two.

The slideshow isn't restarted if a USB stick is plugged in to that TV Pi, because Raspberry
Slideshow would then replace the videos with what's on the stick.

### Grade 9 mode

Turns TVs 1-3 off and mutes the entrance themes, or turns them back on and unmutes. TV 4 (the
hallway) is left alone.

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

[tvs]
host_pattern = "pi-tv{n}.hackerspace.tbl"  # {n} is the TV's number, 1 to 4
port = 22
username = "pi"                     # the TVs share a login
# password = "..."                  # leave out to be asked (recommended)
media_dir = "/home/pi/rs_media"     # the folder Raspberry Slideshow plays
shrines_dir = ""                    # where shrines are saved; empty means Documents\Hackerspace shrines
ffmpeg = ""                         # an ffmpeg.exe to use; empty means download it when needed
```

The Pis usually share a password, so once you've typed it for one Pi, the app tries it on the
others before asking again.

The Pis' remembered identities are kept in a `known_hosts` file in the same folder.

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
| `src/control_panel/panels/tvs.py` | The TVs panel: menus, and `TvPi` for talking to a TV Pi. |
| `src/control_panel/panels/grade9.py` | Grade 9 mode. |
| `src/control_panel/shrine.py` | Making shrines: title cards (Pillow), pictures, slideshows, converting videos. |
| `src/control_panel/ffmpeg.py` | Downloading ffmpeg (pinned, checksum checked) and running it. |
| `src/control_panel/ssh.py` | Running commands and copying files on a Pi (paramiko). |
| `src/control_panel/login.py` | Passwords: config file, then Windows Credential Manager, then ask. |
| `src/control_panel/config.py` | Settings and their defaults. |
| `src/control_panel/ui.py` | Menus, prompts and coloured messages (questionary). |
| `src/control_panel/updater.py` | Checking GitHub for a new release and swapping the exe for it. |

To add a panel, create `src/control_panel/panels/<name>.py` with a `TITLE` and a
`run(config)` function, and add it to `PANELS` in `app.py`. Keep the Pi-talking parts
free of prompts (like `ThemesPi` in `themes.py`) so they're easy to test.

**Releasing:** CI builds `control-panel.exe` on every push; download it from the run's
artifacts on the Actions tab to try it. To publish a release, bump `__version__` in
`src/control_panel/__init__.py`, then push a matching tag (e.g. `v0.3.0`). CI attaches the
exe to a new GitHub release, and everyone's app offers it the next time they open it.

## Legacy control panel

The previous Windows control panel (`main.py`, `panels/`, `*.bat`, `bin/`) isn't used any more.
Everything it did has now moved into the new app, so it can be deleted.

Notes from it for the TV tools: the TV Pis don't fetch anything at start-up any more (they used
to query an SMB server, which stopped working after network changes). Instead, the control panel
makes the mp4s on the user's computer and pushes them to the folder on each Pi where they're
stored. Users save the material to the Hackerspace Teams team themselves for long-term storage;
automating that would open too many security holes.
