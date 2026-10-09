"""Making art shrines: the videos of a student's work that play on the TVs.

A shrine is
- <name>.a.mp4: a title card, then each picture for 8 seconds, crossfading, and
- <name>.z.<video>.mp4: each of their videos and animations, converted so the TVs can play it.

The TVs (Raspberry Slideshow) play every video in their folder in alphabetical order, so
a student's title slideshow (.a) comes just before their videos (.z). Names only use
a-z, 0-9, '.', '_' and '-', because the player skips files with spaces in their names.

Nothing here asks questions, so it's easy to test. The student's own files are only read,
never moved or changed.
"""

from __future__ import annotations

import datetime
import email.message
import http.client
import importlib.resources
import math
import os
import posixpath
import re
import shutil
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from control_panel import __version__, ffmpeg

WIDTH, HEIGHT = SIZE = (1920, 1080)
FPS = 25
SECONDS_PER_PICTURE = 8
FADE_SECONDS = 1
MIN_ANIMATION_SECONDS = 5  # short GIFs are looped so they're on screen long enough to see
SUBJECTS = ["Digital Art", "Digital Photography", "3D Modelling & Animation"]
MAX_DOWNLOAD_BYTES = 2_000_000_000

# Kinds of media
PICTURE = "picture"
ANIMATION = "animation"  # an animated GIF, PNG or WebP
VIDEO = "video"

Image.MAX_IMAGE_PIXELS = 250_000_000  # big photos are fine; Pillow's default is cautious

_SKIP = {"thumbs.db", "desktop.ini", ".ds_store"}
_NOT_MEDIA = {
    ".svg": "drawings (.svg) can't go on the TVs. Export it as a PNG",
    ".pdf": "PDFs can't go on the TVs. Export the pages as pictures",
    ".ppt": "export the slides as pictures", ".pptx": "export the slides as pictures",
    ".psd": "export it as a PNG or JPG",
}


class ShrineError(Exception):
    """Something the person can fix. The message says what."""


# --- Names ---------------------------------------------------------------------------------

def clean_name(text: str, space: str = "_") -> str:
    """A name the TV player can handle: a-z, 0-9, '.', '_' and '-' ('Zoë's Art' -> 'zoes_art')."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"\s+", space, text.strip())
    text = re.sub(r"[^a-z0-9._-]", "", text)
    text = re.sub(r"([._-])[._-]+", r"\1", text)  # no '..' or '_-_'
    return text.strip("._-")  # a name starting with '.' would be hidden


def display_name(username: str) -> str:
    """'tyler.couture' -> 'Tyler Couture', the name shown on the title card."""
    words = [w for w in re.split(r"[._\s]+", username) if w]
    return " ".join("-".join(part.capitalize() for part in w.split("-")) for w in words)


def suggest_tv(name: str) -> int | None:
    """TV 1 for last names A-L, TV 2 for M-Z. name is 'First Last' or 'first.last'."""
    words = re.split(r"[.\s]+", name.strip())
    if len(words) < 2:
        return None
    letter = clean_name(words[-1])[:1]
    if not letter.isalpha():
        return None
    return 1 if letter <= "l" else 2


def natural_key(text: str) -> list:
    """Sort 'pic 2' before 'pic 10', ignoring case."""
    return [(0, int(part), "") if part.isdigit() else (1, 0, part.casefold())
            for part in re.split(r"(\d+)", text) if part]


def this_years_grads(today: datetime.date | None = None) -> int:
    """The grad year of this school year (September to June)."""
    today = today or datetime.date.today()
    return today.year + 1 if today.month >= 8 else today.year


# --- The title card ------------------------------------------------------------------------

@dataclass(frozen=True)
class Title:
    name: str  # in big letters
    subject: str | None = None  # "The <subject> of", above the name
    grad_year: str | None = None  # "Grad <year>", bottom right


CYAN = "#00ffe8"


def title_card(title: Title) -> Image.Image:
    """The first slide, laid out like the old Inkscape template (_template.svg)."""
    card = Image.new("RGB", SIZE, "black")
    draw = ImageDraw.Draw(card)
    if title.subject:
        _draw_spaced(draw, f"The {title.subject} of", centre_x=965, baseline=407,
                     size=56, spacing=10, colour=CYAN, style="Regular")
    _draw_spaced(draw, title.name, centre_x=972, baseline=608, size=176, spacing=0,
                 colour="white", style="Light", outline=True)
    if title.grad_year:
        _draw_spaced(draw, f"Grad {title.grad_year}", centre_x=1648, baseline=1007,
                     size=56, spacing=10, colour=CYAN, style="Regular")
    return card


def _font(size: int, style: str) -> ImageFont.FreeTypeFont:
    with importlib.resources.as_file(
        importlib.resources.files("control_panel") / "assets" / "Ubuntu.ttf"
    ) as path:
        font = ImageFont.truetype(str(path), size)
    font.set_variation_by_name(style)
    return font


def _draw_spaced(draw: ImageDraw.ImageDraw, text: str, *, centre_x: int, baseline: int,
                 size: int, spacing: int, colour: str, style: str,
                 outline: bool = False) -> None:
    """Draw text centred on centre_x, with extra space between letters, shrunk to fit.

    outline thickens the letters a little, like the white outline in the old template.
    """
    margin = 60
    while True:
        font = _font(size, style)
        stroke = round(size / 88) if outline else 0
        widths = [font.getlength(c) for c in text]
        total = sum(widths) + spacing * (len(text) - 1) + 2 * stroke
        # Keep it on screen, even if that means not quite centred on centre_x.
        if total <= WIDTH - 2 * margin or size <= 20:
            break
        size = int(size * 0.95)
    x = min(max(centre_x - total / 2, margin), WIDTH - margin - total)
    if spacing == 0:  # keep the font's kerning
        draw.text((x, baseline), text, font=font, fill=colour, anchor="ls",
                  stroke_width=stroke, stroke_fill=colour)
        return
    for char, width in zip(text, widths, strict=True):
        draw.text((x, baseline), char, font=font, fill=colour, anchor="ls",
                  stroke_width=stroke, stroke_fill=colour)
        x += width + spacing


# --- Finding the student's media -----------------------------------------------------------

@dataclass(frozen=True)
class Media:
    path: Path
    kind: str  # PICTURE, ANIMATION or VIDEO
    label: str  # how to describe it to the person, e.g. its path inside the folder


@dataclass(frozen=True)
class Skipped:
    label: str
    reason: str


def classify(ffmpeg_path: Path, path: Path) -> str:
    """PICTURE, ANIMATION or VIDEO, going by what's in the file, not its name.

    Raises ShrineError saying why if it's none of those.
    """
    try:
        with Image.open(path) as image:
            if getattr(image, "is_animated", False) and image.format in ("GIF", "PNG", "WEBP"):
                return ANIMATION
            return PICTURE
    except Image.DecompressionBombError as e:
        raise ShrineError("the picture is too big (over 250 megapixels)") from e
    except (OSError, ValueError, SyntaxError):
        pass  # not a picture Pillow knows; maybe a video
    reason = _NOT_MEDIA.get(path.suffix.lower())
    if reason:
        raise ShrineError(reason)
    probe = ffmpeg.probe(ffmpeg_path, path)
    if probe.has_video and probe.duration and probe.duration >= 0.5:
        return VIDEO
    if path.suffix.lower() in (".heic", ".heif"):
        raise ShrineError("iPhone photos (.heic) need converting to JPG first")
    raise ShrineError("it isn't a picture or video")


def gather(ffmpeg_path: Path, source: Path, work_dir: Path) -> tuple[list[Media], list[Skipped]]:
    """Every picture and video in a folder (and its subfolders), a file, or a zip file."""
    found: list[Media] = []
    skipped: list[Skipped] = []
    if source.is_dir():
        files = [(path, path.relative_to(source).as_posix()) for path in _walk(source)]
    else:
        files = [(source, source.name)]
    for path, label in files:
        if path.suffix.lower() == ".zip" and zipfile.is_zipfile(path):
            inside, inside_skipped = gather(ffmpeg_path, _unzip(path, work_dir), work_dir)
            found += [Media(m.path, m.kind, f"{label}/{m.label}") for m in inside]
            skipped += [Skipped(f"{label}/{s.label}", s.reason) for s in inside_skipped]
            continue
        try:
            found.append(Media(path, classify(ffmpeg_path, path), label))
        except ShrineError as e:
            skipped.append(Skipped(label, str(e)))
    return found, skipped


def _walk(folder: Path) -> Iterator[Path]:
    """Files in folder and its subfolders, in natural order, leaving out hidden/system files."""
    entries = sorted(folder.iterdir(), key=lambda p: natural_key(p.name))
    for entry in entries:
        if entry.name.startswith((".", "~$")) or entry.name.lower() in _SKIP:
            continue
        if entry.is_dir():
            yield from _walk(entry)
        elif entry.is_file():
            yield entry


def _unzip(path: Path, work_dir: Path) -> Path:
    target = work_dir / f"zip-{len(list(work_dir.glob('zip-*')))}"
    try:
        with zipfile.ZipFile(path) as archive:
            archive.extractall(target)  # Python keeps the files inside target
    except (OSError, zipfile.BadZipFile, RuntimeError) as e:  # RuntimeError: has a password
        raise ShrineError(f"Couldn't open {path.name}: {e}") from e
    return target


# --- Links ---------------------------------------------------------------------------------

def direct_link(url: str) -> str:
    """Turn a share link into one that downloads the file, where we know how."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    query = urllib.parse.parse_qs(parts.query)
    if host == "drive.google.com":
        match = re.search(r"/file/d/([\w-]+)", parts.path)
        file_id = match[1] if match else (query.get("id") or [None])[0]
        if file_id:
            return f"https://drive.google.com/uc?export=download&id={file_id}"
    if host.endswith("dropbox.com"):
        query["dl"] = ["1"]
        return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query, True)))
    if host == "1drv.ms" or host.endswith(("onedrive.live.com", "sharepoint.com")):
        query["download"] = ["1"]
        return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query, True)))
    return url


def download_link(url: str, folder: Path,
                  progress: Callable[[int, int | None], None] | None = None) -> Path:
    """Download a picture, video or zip file from a link into folder. Returns the file."""
    request = urllib.request.Request(
        direct_link(url), headers={"User-Agent": f"hackerspace-control-panel/{__version__}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.headers.get_content_type() == "text/html":
                raise ShrineError(_web_page_message(url))
            name = _download_name(response.headers, response.url) or "download"
            path = _unused(folder / name)
            total = response.length
            done = 0
            with path.open("wb") as f:
                while chunk := response.read(1 << 16):
                    done += len(chunk)
                    if done > MAX_DOWNLOAD_BYTES:
                        raise ShrineError("That file is too big (over 2 GB).")
                    f.write(chunk)
                    if progress:
                        progress(done, total)
            if response.length:  # Content-Length said there was more
                raise ShrineError("The download was cut off part way. Try again.")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ShrineError(_web_page_message(url)) from e
        raise ShrineError(f"The link didn't work (error {e.code}: {e.reason}).") from e
    except http.client.HTTPException as e:
        raise ShrineError("Couldn't download that link (the reply was broken or cut off).") from e
    except (OSError, ValueError) as e:
        raise ShrineError(f"Couldn't download that link: {getattr(e, 'reason', e)}") from e
    return path


def _web_page_message(url: str) -> str:
    host = urllib.parse.urlsplit(url).hostname or ""
    if host.endswith(("sharepoint.com", "onedrive.live.com", "1drv.ms")) or "teams" in host:
        return ("That link needs a school sign-in, which this app can't do. Download the file "
                "or folder from OneDrive/Teams (a folder comes as a .zip), then drag it in.")
    if host == "drive.google.com":
        return ("Google Drive didn't hand over the file (it may be private, or too big to "
                "download without a click). Download it yourself, then drag it in.")
    return ("That link opens a web page, not a picture or video. Use a link to the file "
            "itself, or download it and drag it in.")


def _download_name(headers: email.message.Message, url: str) -> str:
    message = email.message.Message()
    message["content-disposition"] = headers.get("content-disposition", "")
    name = message.get_filename() or posixpath.basename(
        urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    )
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip(". ")  # no folders, no '..'
    return re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)  # not allowed in Windows file names


def _unused(path: Path) -> Path:
    n = 2
    candidate = path
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        n += 1
    return candidate


# --- Making the videos ---------------------------------------------------------------------

def prepare_picture(source: Path, slide: Path) -> None:
    """Save a picture as a 1920x1080 slide: upright, transparency on black, letterboxed."""
    with Image.open(source) as image:
        image.draft("RGB", SIZE)  # big JPEGs load much faster at a smaller size
        image.seek(0)  # the first frame, if it's animated
        upright = ImageOps.exif_transpose(image)
        picture = _on_black(upright)
    picture = ImageOps.contain(picture, SIZE, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", SIZE, "black")
    canvas.paste(picture, ((WIDTH - picture.width) // 2, (HEIGHT - picture.height) // 2))
    canvas.save(slide, compress_level=1)


def _on_black(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    background = Image.new("RGBA", rgba.size, "black")
    return Image.alpha_composite(background, rgba).convert("RGB")


def slideshow_seconds(slide_count: int) -> int:
    return SECONDS_PER_PICTURE * slide_count + FADE_SECONDS


def slideshow_args(slides: list[Path], output: Path) -> list[str]:
    """ffmpeg arguments for a slideshow of 1920x1080 slides.

    Slide k is fully on screen from 8k+1 s to 8k+8 s, crossfading into the next for 1 s.
    The whole thing fades in from black and out to black, so it's 8n+1 seconds long.
    Each slide is read as 1 frame a second and repeated (fps) so it's quick to make.
    """
    step, fade = SECONDS_PER_PICTURE, FADE_SECONDS
    args: list[str] = []
    for slide in slides:
        args += ["-loop", "1", "-framerate", "1", "-t", str(step + fade), "-i", str(slide)]
    seconds = slideshow_seconds(len(slides))
    graph = [f"[{i}:v]fps={FPS},format=yuv420p,setsar=1[s{i}]" for i in range(len(slides))]
    last = "s0"
    for i in range(1, len(slides)):
        graph.append(f"[{last}][s{i}]xfade=transition=fade:duration={fade}:offset={step * i}"
                     f"[x{i}]")
        last = f"x{i}"
    graph.append(f"[{last}]fade=t=in:d={fade},fade=t=out:st={seconds - fade}:d={fade}[v]")
    return [*args, "-filter_complex", ";".join(graph), "-map", "[v]",
            "-t", str(seconds), *_h264(), str(output)]


def video_args(source: Path, output: Path, *, scale_flags: str = "bicubic",
               max_seconds: float | None = None) -> list[str]:
    """ffmpeg arguments to convert a video into one the TVs play: 1920x1080, 25 fps, H.264,
    no sound."""
    fit = (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease:flags={scale_flags},"
           f"pad={WIDTH}:{HEIGHT}:-1:-1:color=black,setsar=1,fps={FPS},format=yuv420p")
    limit = ["-t", f"{max_seconds:.3f}"] if max_seconds else []
    return ["-i", str(source), "-map", "0:v:0", "-vf", fit, "-an", "-sn", "-dn",
            "-map_metadata", "-1", *limit, *_h264(), str(output)]


def _h264() -> list[str]:
    # What the old movie maker made, which the TVs are known to play.
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high",
            "-pix_fmt", "yuv420p", "-r", str(FPS), "-movflags", "+faststart"]


Frames = list[tuple[Path, float]]  # (frame, how many seconds it's shown)


def animation_frames(source: Path, folder: Path) -> tuple[Frames, tuple[int, int]]:
    """Save an animation's frames, on black. Returns ([(frame, seconds)], frame size).

    ffmpeg can't read animated WebP, and gets GIF transparency and timing wrong, so
    Pillow does this part.
    """
    folder.mkdir(parents=True, exist_ok=True)
    frames = []
    with Image.open(source) as image:
        size = image.size
        for index in range(getattr(image, "n_frames", 1)):
            image.seek(index)
            frame = folder / f"{index:05d}.png"
            _on_black(image).save(frame, compress_level=1)
            # Browsers show frames of 10 ms or less for 100 ms; so do we.
            duration = image.info.get("duration") or 0
            frames.append((frame, duration / 1000 if duration > 10 else 0.1))
    return frames, size


def write_playlist(frames: Frames, loops: int, playlist: Path) -> None:
    """An ffmpeg 'concat' list that plays the frames, with their timings, loops times."""
    lines = ["ffconcat version 1.0"]
    for _ in range(loops):
        for frame, seconds in frames:
            lines += [f"file '{frame.name}'", f"duration {seconds:.3f}"]
    lines.append(f"file '{frames[-1][0].name}'")  # the last duration only counts if repeated
    playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")


def video_file_name(name: str, label: str, taken: set[str]) -> str:
    """<name>.z.<video's name>.mp4, made unique among taken (which it's added to)."""
    stem = clean_name(posixpath.splitext(posixpath.basename(label))[0]) or "video"
    candidate, n = f"{name}.z.{stem}.mp4", 2
    while candidate in taken:
        candidate, n = f"{name}.z.{stem}-{n}.mp4", n + 1
    taken.add(candidate)
    return candidate


Progress = Callable[[str], AbstractContextManager[Callable[[float], None]]]


def make(ffmpeg_path: Path, name: str, title: Title | None, media: list[Media],
         work_dir: Path, progress: Progress) -> tuple[list[Path], list[Skipped]]:
    """Make the shrine's videos in work_dir. Returns (the videos, what couldn't be used).

    progress(label) is a context manager giving a function to report progress (0 to 1).
    """
    made: list[Path] = []
    problems: list[Skipped] = []
    slides: list[Path] = []
    slides_dir = work_dir / "slides"
    slides_dir.mkdir(parents=True, exist_ok=True)
    if title:
        slides.append(slides_dir / "0000.png")
        title_card(title).save(slides[0])

    pictures = [m for m in media if m.kind == PICTURE]
    if pictures:
        with progress(f"Getting {len(pictures)} picture(s) ready") as update:
            for i, picture in enumerate(pictures, start=1):
                slide = slides_dir / f"{i:04d}.png"
                try:
                    prepare_picture(picture.path, slide)
                    slides.append(slide)
                except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as e:
                    problems.append(Skipped(picture.label, f"couldn't read it ({e})"))
                update(i / len(pictures))

    if slides:
        output = work_dir / f"{name}.a.mp4"
        with progress(f"Making the slideshow ({len(slides)} slides)") as update:
            ffmpeg.run(ffmpeg_path, slideshow_args(slides, output),
                       seconds=slideshow_seconds(len(slides)), progress=update)
        made.append(output)

    taken: set[str] = set()
    for item in (m for m in media if m.kind in (VIDEO, ANIMATION)):
        output = work_dir / video_file_name(name, item.label, taken)
        try:
            with progress(f"Converting {item.label}") as update:
                _convert(ffmpeg_path, item, output, work_dir, update)
            made.append(output)
        except (ffmpeg.FfmpegError, OSError, ValueError, SyntaxError) as e:
            problems.append(Skipped(item.label, f"couldn't convert it ({e})"))
    return made, problems


def _convert(ffmpeg_path: Path, item: Media, output: Path, work_dir: Path,
             update: Callable[[float], None]) -> None:
    if item.kind == VIDEO:
        seconds = ffmpeg.probe(ffmpeg_path, item.path).duration
        ffmpeg.run(ffmpeg_path, video_args(item.path, output), seconds=seconds, progress=update)
        return
    folder = work_dir / f"frames-{output.stem}"
    frames, (width, height) = animation_frames(item.path, folder)
    once = sum(seconds for _, seconds in frames)
    loops = max(1, math.ceil(MIN_ANIMATION_SECONDS / once))
    playlist = folder / "frames.txt"
    write_playlist(frames, loops, playlist)
    # Pixel art stays sharp when it's made much bigger.
    sharp = min(WIDTH / width, HEIGHT / height) >= 2
    args = video_args(playlist, output, scale_flags="neighbor" if sharp else "lanczos",
                      max_seconds=once * loops)
    ffmpeg.run(ffmpeg_path, ["-f", "concat", *args], seconds=once * loops, progress=update)
    shutil.rmtree(folder, ignore_errors=True)


def remove_old(folder: Path, name: str) -> None:
    """Delete a shrine's videos from a folder, before saving new ones there."""
    for path in folder.glob("*.mp4"):
        if is_part_of(path.name, name):
            os.remove(path)


def is_part_of(file_name: str, name: str) -> bool:
    """Whether a file on a TV (or in a folder) belongs to the shrine called name."""
    return file_name.startswith((f"{name}.a.", f"{name}.z.")) and \
        file_name.lower().endswith(".mp4")
