# Hackerspace Control Panel

Control the Timberline Hackerspace Raspberry Pis from a lab computer.

> **Status:** this is a rewrite of the old control panel. It does the **TV art shrines**, the
> **entrance theme machine** (pi-themes) and **grade 9 mode**.

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

The main menu has three panels: **TV Art Shrines**, **Entrance Theme Machine** and **Grade 9
Mode**. Each panel clears the screen and shows its heading, then a numbered menu: type a number
and press Enter, or `q` to go back (or quit, from the main menu). Questions end with
`, or (q)uit:`, and show what Enter does if it does something, like
`Type their grad year (Enter = 2027), or (q)uit:`. Yes-or-no questions are `(Y/n)` or `(y/N)`,
the capital letter being what Enter means. Where you can pick several TVs, type their numbers,
like `1 4` or `2-3` or `all`. When a task is done and has something to read, press Enter to go back to the panel (if its last step was a question, like `Add another theme? (Y/n)`, answering it goes straight back). In a menu that suggests an item, like the TV a shrine goes on, it shows `Choose an item (Enter = 2), or (q)uit:`.

The first time you connect to a Pi you'll be asked for its password. If you're on your own
Windows login (not a shared one), you can have Windows remember it in Credential Manager. The
app also remembers each Pi's identity the first time it connects. If that ever changes, it warns
you before sending the password, so another computer can't pretend to be a Pi to collect it.
That's expected after a Pi is re-installed; you'll be asked whether to trust it.

### TV Art Shrines

The four TV Pis (`pi-tv1` to `pi-tv4`) run Raspberry Slideshow, which plays every video in
`/home/pi/rs_media` in alphabetical order, over and over. Each student's (or collection's)
**shrine** is a set of videos:

- `firstname.lastname.a.mp4`: a title card ("The Digital Art of ... Grad 2027"), then each of
  their pictures for 8 seconds, crossfading from one to the next.
- `firstname.lastname.z.<name>.mp4`: each of their videos and animated GIFs, converted so the TVs
  can play them (1920x1080, no sound). Short GIFs are looped to at least 5 seconds.

**pi-files is the only place the art lives.** Every shrine's original art is kept there, with
the videos made from it. Whenever a shrine's art is added, deleted or removed, the app makes that
shrine's videos again from its art (on this computer, with ffmpeg), then copies them to its TV
and the hallway, saying what it copies and takes off as it goes. Nothing is kept on this
computer. A video that's already in the TVs' format (H.264, at most 1920x1080 and 30 fps, no
sound) is used as it is, under the same name; others are converted, and the app says why.

| TV | What's on it |
| --- | --- |
| TV 1 | The shrines in pi-files' `tv1` folder: original art, last names A-L |
| TV 2 | The shrines in `tv2`: original art, last names M-Z |
| TV 3 | The shrines in `tv3`: demos, tutorials and Skills Canada |
| TV 4 | The hallway: every shrine, from `tv1` to `tv4` |

| Menu item | What it does |
| --- | --- |
| Make a shrine | For a student (username, name, grad year, subject) or a collection (e.g. Skills Canada, with or without a title card). Pick its TV, then drag in their folder or files, or paste links to pictures, videos or `.zip` files (a OneDrive/Teams folder downloads as a `.zip`). The student's own files are never moved or changed. |
| Add art to a shrine | Type part of their username, then drag in or paste the new art. Art that's already there is skipped, so you can drag in their whole folder again; if some pieces have the same names as old ones, it asks whether to replace them (the old ones go to `backup`) or keep both. |
| List, View, and/or Delete a Shrine's Art | Search for a shrine by name, or list the shrines on a TV and pick one. Then it lists their art (title card first, then the pictures in slideshow order, then videos). Pick a piece and it opens in its usual app (Photos, a video player); then you can delete it (it's moved to `backup`). |
| Rename a shrine (e.g. to a different username) | Renames its folder on pi-files and its art and videos with it (`old.name.z.sun.jpg` becomes `new.name.z.sun.jpg`), so nothing is made again. It can move to another TV too (Enter keeps it where it is). If it has a title card, you can make a new one with the new name. Then its old videos come off the TVs and the renamed ones go on. |
| Remove a shrine | Takes it off the TVs, and either moves its art to `backup` (so it can be put back) or deletes it for good. |
| Update the TVs from pi-files | Checks every shrine the chosen TVs show: tidies hightower's loose files, makes any videos pi-files is missing (e.g. for art copied there by hand, or from hightower), then makes the TVs match. Videos on a TV that aren't on pi-files are listed, and you choose: add them to pi-files (each to its student's shrine, so they stay), back them up to pi-files and take them off, or leave them. |
| Turn TVs on or off | Some or all of them, at the same time (using the TV's HDMI-CEC remote control commands). |
| Restart a TV's slideshow, or reboot it | Restarting the slideshow is quick and picks up new or deleted videos. |
| TV Art Shrines documentation | A link to this page. |

The first time videos are made, the app downloads **ffmpeg** (a free video tool, about 31 MB)
and keeps it in `%LOCALAPPDATA%\hackerspace-control-panel`.

The slideshow isn't restarted if a USB stick is plugged in to that TV Pi, because Raspberry
Slideshow would then replace the videos with what's on the stick.

#### pi-files

**pi-files** is a Raspberry Pi with an external drive, like hightower was for the old Linux
control panel. On its drive there's a folder for each TV, and in it a folder for each student
or collection:

```
/mnt/ssd/tv1/ann.lee/ann.lee.a.title.png   their title card (always the first slide)
/mnt/ssd/tv1/ann.lee/ann.lee.z.sunset.jpg  each picture and video (pictures are shown in the
/mnt/ssd/tv1/ann.lee/...                   order of their names)
/mnt/ssd/tv1/ann.lee/.videos/              the videos made from them, which go on the TVs
/mnt/ssd/tv3/skills-canada-2026/...        a collection
/mnt/ssd/backup/                           art that was replaced, deleted or removed, and
                                           videos taken off a TV (moved here, kept)
```

Art copied there by hand can have any names. hightower's art can be copied over as it is:
videos it kept loose in a TV's folder (`tv1/ann.lee.z.clip.mp4`) are moved into the student's
folder, and its old slideshows (`tv1/ann.lee.a.mp4`) into `backup`, as they're made again from
the pictures. Then use *Update the TVs from pi-files*. Other `.mp4` videos left loose in a TV's
folder, that aren't anyone's (like `tv3/0_RubeGoldberg-Webb.mp4`), play on that TV as they
are. Files that aren't art, like hightower's `.py` scripts, are ignored.

To set one up: Raspberry Pi OS Lite, with the hostname `pi-files`, the user `pi` with the same
password as the TVs, and SSH turned on (all in Raspberry Pi Imager's settings). Format the drive as
ext4 and mount it at `/mnt/ssd` from `/etc/fstab` (with `nofail`, so the Pi still starts without
it). Files copied there with `sudo` belong to root, so then run `sudo chown -R pi: /mnt/ssd` (the
app says so if it can't save there). The app won't keep art anywhere that isn't on the external
drive. It's worth backing up the drive now and then.

### Entrance Theme Machine

| Menu item | What it does |
| --- | --- |
| Add a new theme | From a link to an mp3, or an mp3 file dragged into the window. Checks that it really is an mp3, suggests the code from the file name. If the code is taken, you can use another code or replace that theme. |
| Remove a theme | Type its code. Then either keep a backup on pi-themes (in `removed/` next to the themes, where the player can't play it), or delete it for good. |
| Play a theme | Type codes to play themes on the entrance speaker, just like the keypad. It runs its own copy of the player, so spam-block and admin (`*`) codes here don't affect the keypad. Leaving stops anything still playing. |
| List and search themes | Shows every theme code on the Pi's USB drive, then type part of a code to see only the codes that contain it. |
| Toggle mute | Mutes the speaker, or unmutes it if it's muted. |
| Say something (text to speech) | Through the entrance speaker. |
| Reboot pi-themes | Turns it off and on again. |
| pi-themes documentation | A link to the player, [timberline-secondary/themes](https://github.com/timberline-secondary/themes). |

Nothing on pi-themes needs to change to use this app.

### Grade 9 Mode

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
ffmpeg = ""                         # an ffmpeg.exe to use; empty means download it when needed
art_host = "pi-files.hackerspace.tbl"  # the Pi that keeps the shrines' art (same login as the TVs)
# (if that name isn't found, its short name, e.g. pi-files, and pi-files.local are tried)
art_dir = "/mnt/ssd"                # its external drive, with a folder for each TV (tv1 to tv4)
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
| `src/control_panel/panels/themes.py` | The Entrance Theme Machine panel. |
| `src/control_panel/panels/tvs.py` | The TV Art Shrines panel: making each shrine's videos from its art, keeping the TVs the same as pi-files, and `TvPi` for talking to a TV Pi. |
| `src/control_panel/art.py` | pi-files: where the shrines' art, and the videos made from it, are kept. |
| `src/control_panel/panels/grade9.py` | Grade 9 mode. |
| `src/control_panel/shrine.py` | Making shrines: title cards (Pillow), pictures, slideshows, converting videos. |
| `src/control_panel/ffmpeg.py` | Downloading ffmpeg (pinned, checksum checked) and running it. |
| `src/control_panel/ssh.py` | Running commands and copying files on a Pi (paramiko). |
| `src/control_panel/login.py` | Passwords: config file, then Windows Credential Manager, then ask. |
| `src/control_panel/config.py` | Settings and their defaults. |
| `src/control_panel/ui.py` | Menus, questions and coloured messages (plain `input()` and `print()`). |
| `src/control_panel/updater.py` | Checking GitHub for a new release and swapping the exe for it. |

To add a panel, create `src/control_panel/panels/<name>.py` with a `TITLE` and a
`run(config)` function, and add it to `PANELS` in `app.py`. Keep the Pi-talking parts
free of prompts (like `ThemesPi` in `themes.py`) so they're easy to test.

**Releasing:** CI builds `control-panel.exe` on every push; download it from the run's
artifacts on the Actions tab to try it. To publish a release, bump `__version__` in
`src/control_panel/__init__.py` in a PR. When it's merged, CI builds the exe, tags the commit
(e.g. `v0.5.0`) and publishes it as a GitHub release, and everyone's app offers it the next time
they open it. (It does this whenever the newest commit on `main` has a version higher than any
released so far, so merges that keep the same version publish nothing.) If that CI run fails,
re-run it from the Actions tab. Pushing a tag like `v0.5.0` by hand also still works.

## Legacy control panel

The previous Windows control panel (`main.py`, `panels/`, `*.bat`, `bin/`) isn't used any more.
Everything it did has now moved into the new app, so it can be deleted.

Notes from it for the TV tools: the TV Pis don't fetch anything at start-up any more (they used
to query an SMB server, which stopped working after network changes). Instead, the control panel
makes the mp4s on the user's computer and pushes them to the folder on each Pi where they're
stored. Users save the material to the Hackerspace Teams team themselves for long-term storage;
automating that would open too many security holes.
