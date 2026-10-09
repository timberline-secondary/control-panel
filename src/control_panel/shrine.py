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
import warnings
import zipfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat

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

MAX_PICTURE_PIXELS = 120_000_000  # bigger pictures need gigabytes of memory to prepare
MAX_UNZIPPED_BYTES = 4_000_000_000  # in all, from the zip files in one folder or link
MAX_ZIP_DEPTH = 2  # a zip inside a zip is opened, but not a zip inside that
Image.MAX_IMAGE_PIXELS = MAX_PICTURE_PIXELS  # Pillow refuses twice this; classify() this
warnings.simplefilter("ignore", Image.DecompressionBombWarning)  # classify() says it nicely

_SKIP = {"thumbs.db", "desktop.ini", ".ds_store"}
_NOT_PICTURES = {"MPEG"}  # Pillow reads these, but they're videos
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
    opened = False
    try:
        with Image.open(path) as image:
            opened = True
            if image.format not in _NOT_PICTURES:
                if image.width * image.height > MAX_PICTURE_PIXELS:
                    raise ShrineError(f"the picture is too big ({image.width}x{image.height}). "
                                      "Make it smaller, e.g. 4000 pixels wide, first")
                if getattr(image, "is_animated", False) and \
                        image.format in ("GIF", "PNG", "WEBP"):
                    return ANIMATION
                return PICTURE
            opened = False
    except ShrineError:
        raise
    except Image.DecompressionBombError as e:
        raise ShrineError("the picture is too big. Make it smaller first") from e
    except Exception as e:  # Pillow raises all sorts for damaged files
        if opened:
            raise ShrineError("it seems to be damaged (maybe it was only partly copied)") from e
    reason = _NOT_MEDIA.get(path.suffix.lower())
    if reason:
        raise ShrineError(reason)
    if path.suffix.lower() in (".heic", ".heif"):
        raise ShrineError("iPhone photos (.heic) need converting to JPG first")
    probe = ffmpeg.probe(ffmpeg_path, path)
    # Some recordings (e.g. from a web browser) don't say how long they are.
    if probe.has_video and (probe.duration is None or probe.duration >= 0.5):
        return VIDEO
    raise ShrineError("it isn't a picture or video")


def gather(ffmpeg_path: Path, source: Path, work_dir: Path, *, depth: int = 0,
           budget: list[int] | None = None) -> tuple[list[Media], list[Skipped]]:
    """Every picture and video in a folder (and its subfolders), a file, or a zip file.

    One file that can't be used (a zip with a password, a folder that can't be opened...)
    is listed in what's skipped, rather than stopping everything.
    """
    budget = [MAX_UNZIPPED_BYTES] if budget is None else budget  # shared by nested calls
    found: list[Media] = []
    skipped: list[Skipped] = []
    if source.is_dir():
        problems: list[tuple[Path, str]] = []
        files = [(path, path.relative_to(source).as_posix()) for path in _walk(source, problems)]
        skipped += [Skipped(folder.relative_to(source).as_posix() if folder != source
                            else source.name, reason) for folder, reason in problems]
    else:
        files = [(source, source.name)]
    for path, label in files:
        try:
            if path.suffix.lower() == ".zip" and zipfile.is_zipfile(path):
                if depth >= MAX_ZIP_DEPTH:
                    raise ShrineError("it's a zip file inside a zip file inside a zip file")
                inside, inside_skipped = gather(ffmpeg_path, _unzip(path, work_dir, budget),
                                                work_dir, depth=depth + 1, budget=budget)
                found += [Media(m.path, m.kind, f"{label}/{m.label}") for m in inside]
                skipped += [Skipped(f"{label}/{s.label}", s.reason) for s in inside_skipped]
            else:
                found.append(Media(path, classify(ffmpeg_path, path), label))
        except ShrineError as e:
            skipped.append(Skipped(label, str(e)))
        except OSError as e:
            skipped.append(Skipped(label, f"couldn't read it ({e.strerror or e})"))
    return found, skipped


def _walk(folder: Path, problems: list[tuple[Path, str]]) -> Iterator[Path]:
    """Files in folder and its subfolders, in natural order, leaving out hidden/system files.
    Folders that can't be opened are added to problems."""
    try:
        entries = sorted(folder.iterdir(), key=lambda p: natural_key(p.name))
    except OSError as e:
        problems.append((folder, f"couldn't open the folder ({e.strerror or e})"))
        return
    for entry in entries:
        if entry.name.startswith((".", "~$")) or entry.name.lower() in _SKIP:
            continue
        if entry.is_dir():
            if not entry.is_symlink():  # a link back up would go round forever
                yield from _walk(entry, problems)
        elif entry.is_file():
            yield entry


def _unzip(path: Path, work_dir: Path, budget: list[int]) -> Path:
    target = work_dir / f"zip-{len(list(work_dir.glob('zip-*')))}"
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            size = sum(member.file_size for member in members)  # Python won't unzip more
            if size > budget[0] or len(members) > 10_000:
                raise ShrineError("it's too big to unzip (over 4 GB, or over 10,000 files)")
            budget[0] -= size
            archive.extractall(target)  # Python keeps the files inside target
    except ShrineError:
        raise
    except Exception as e:  # damaged data raises zlib/lzma errors as well as the usual ones
        if "password" in str(e):
            raise ShrineError("it has a password. Unzip it yourself, then drag in the "
                              "folder") from e
        raise ShrineError(f"couldn't unzip it ({e})") from e
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
    """Save a picture as a 1920x1080 slide: upright, letterboxed, transparency filled in."""
    with Image.open(source) as image:
        image.draft("RGB", SIZE)  # big JPEGs load much faster at a smaller size
        picture = _rgba(ImageOps.exif_transpose(image))
    picture = ImageOps.contain(picture, SIZE, Image.Resampling.LANCZOS)
    picture = flatten(picture, background_for(picture))
    canvas = Image.new("RGB", SIZE, "black")
    canvas.paste(picture, ((WIDTH - picture.width) // 2, (HEIGHT - picture.height) // 2))
    canvas.save(slide, compress_level=1)


def _rgba(image: Image.Image) -> Image.Image:
    """Any picture as RGBA. 16-bit greys are scaled down, not clipped to white."""
    if image.mode in ("I;16", "I;16L", "I;16B", "I;16N", "I"):
        image = image.convert("I").point(lambda value: value / 256).convert("L")
    elif image.mode == "F":
        low, high = image.getextrema()
        image = image.point(lambda value: (value - low) * 255 / ((high - low) or 1)).convert("L")
    return image.convert("RGBA")


def background_for(picture: Image.Image) -> str:
    """What to show through see-through parts: black, unless what's visible is dark (like
    black line art on a clear background), which would vanish on black."""
    alpha = picture.getchannel("A")
    if alpha.getextrema()[0] == 255:
        return "black"  # nothing is see-through
    visible = alpha.point(lambda a: 255 if a >= 128 else 0)
    brightness = ImageStat.Stat(picture.convert("L"), mask=visible)
    return "white" if brightness.count[0] and brightness.mean[0] < 60 else "black"


def flatten(picture: Image.Image, background: str) -> Image.Image:
    canvas = Image.new("RGBA", picture.size, background)
    return Image.alpha_composite(canvas, picture).convert("RGB")


def slideshow_seconds(slide_count: int) -> int:
    return SECONDS_PER_PICTURE * slide_count + FADE_SECONDS


def slideshow_frames(slides: list[Path]) -> Iterator[bytes]:
    """Every frame of a slideshow of 1920x1080 slides, as raw RGB.

    Slide k comes on at 8k seconds, fading in over the one before for 1 s. The first fades
    in from black and the last fades out to black, so it's 8n+1 seconds long. Making the
    frames here (rather than in one big ffmpeg filter) keeps memory use the same however
    many slides there are.
    """
    fade = FPS * FADE_SECONDS
    hold = FPS * SECONDS_PER_PICTURE - fade
    black = Image.new("RGB", SIZE, "black")
    previous = black
    for slide in slides:
        with Image.open(slide) as image:
            current = image.convert("RGB")
        if current.size != SIZE:
            current = current.resize(SIZE)
        for i in range(fade):
            yield Image.blend(previous, current, i / fade).tobytes()
        still = current.tobytes()
        for _ in range(hold):
            yield still
        previous = current
    for i in range(fade):
        yield Image.blend(previous, black, i / fade).tobytes()


# Pictures are RGB; TVs expect HD video's colours (BT.709, TV range), so convert and say so.
_FROM_RGB = "out_color_matrix=bt709:out_range=tv"
_BT709 = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
          "-color_range", "tv"]


def slideshow_args(output: Path) -> list[str]:
    """ffmpeg arguments to encode slideshow_frames() (fed to it through stdin)."""
    return ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS),
            "-i", "pipe:0", "-vf", f"scale={_FROM_RGB},format=yuv420p", *_BT709, *_h264(),
            str(output)]


def video_args(source: Path, output: Path, *, input_options: tuple[str, ...] = (),
               scale_flags: str = "bicubic", from_rgb: bool = False,
               max_seconds: float | None = None) -> list[str]:
    """ffmpeg arguments to convert a video into one the TVs play: 1920x1080, 25 fps, H.264,
    no sound."""
    # Square the pixels first: some cameras store wide video in narrow pixels. (Only then:
    # resizing square pixels would blur pixel art.)
    fit = (f"scale=w='if(eq(sar,1),iw,trunc(iw*sar/2)*2)':h=ih:flags={scale_flags},setsar=1,"
           f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease:flags={scale_flags}"
           f"{':' + _FROM_RGB if from_rgb else ''},"
           f"pad={WIDTH}:{HEIGHT}:-1:-1:color=black,setsar=1,fps={FPS},format=yuv420p")
    limit = ["-t", f"{max_seconds:.3f}"] if max_seconds else []
    return [*input_options, "-i", str(source), "-map", "0:v:0", "-vf", fit, "-an", "-sn", "-dn",
            "-map_metadata", "-1", *limit, *(_BT709 if from_rgb else []), *_h264(), str(output)]


def _h264() -> list[str]:
    # What the old movie maker made, which the TVs are known to play.
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high",
            "-pix_fmt", "yuv420p", "-r", str(FPS), "-movflags", "+faststart"]


Frames = list[tuple[Path, float]]  # (frame, how many seconds it's shown)


def animation_frames(source: Path, folder: Path) -> tuple[Frames, tuple[int, int]]:
    """Save an animation's frames, see-through parts filled in. Returns
    ([(frame, seconds)], frame size).

    ffmpeg can't read animated WebP, and gets GIF transparency and timing wrong, so
    Pillow does this part.
    """
    folder.mkdir(parents=True, exist_ok=True)
    frames = []
    background = None
    with Image.open(source) as image:
        size = image.size
        for index in range(getattr(image, "n_frames", 1)):
            image.seek(index)
            picture = _rgba(image)
            background = background or background_for(picture)  # the same for every frame
            frame = folder / f"{index:05d}.png"
            flatten(picture, background).save(frame, compress_level=1)
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
    """<name>.z.<video's name>.mp4, made unique among taken (which it's added to).

    The video's part has no dots, so it's always clear whose shrine a file is part of.
    """
    stem = clean_name(posixpath.splitext(posixpath.basename(label))[0]).replace(".", "-")
    stem = stem or "video"
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
    One file that can't be used is reported, rather than stopping the whole shrine.
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
                except Exception as e:  # a damaged or odd file; Pillow raises all sorts
                    problems.append(Skipped(picture.label, f"couldn't read it ({e})"))
                update(i / len(pictures))

    if slides:
        output = work_dir / f"{name}.a.mp4"
        try:
            with progress(f"Making the slideshow ({len(slides)} slides)") as update:
                ffmpeg.encode(ffmpeg_path, slideshow_frames(slides),
                              FPS * slideshow_seconds(len(slides)), slideshow_args(output),
                              progress=update)
            made.append(output)
        except Exception as e:
            problems.append(Skipped("the slideshow", f"couldn't make it ({e})"))

    taken: set[str] = set()
    for item in (m for m in media if m.kind in (VIDEO, ANIMATION)):
        output = work_dir / video_file_name(name, item.label, taken)
        try:
            with progress(f"Converting {item.label}") as update:
                _convert(ffmpeg_path, item, output, work_dir, update)
            made.append(output)
        except Exception as e:
            problems.append(Skipped(item.label, f"couldn't convert it ({e})"))
    return made, problems


def _convert(ffmpeg_path: Path, item: Media, output: Path, work_dir: Path,
             update: Callable[[float], None]) -> None:
    if item.kind == VIDEO:
        seconds = ffmpeg.probe(ffmpeg_path, item.path).duration
        args = video_args(item.path, output,
                          input_options=("-format_whitelist", ffmpeg.VIDEO_FORMATS))
        ffmpeg.run(ffmpeg_path, args, seconds=seconds, progress=update)
        return
    folder = work_dir / f"frames-{output.stem}"
    frames, (width, height) = animation_frames(item.path, folder)
    once = sum(seconds for _, seconds in frames)
    loops = max(1, math.ceil(MIN_ANIMATION_SECONDS / once))
    playlist = folder / "frames.txt"
    write_playlist(frames, loops, playlist)
    # Pixel art stays sharp when it's made much bigger.
    sharp = min(WIDTH / width, HEIGHT / height) >= 2
    args = video_args(playlist, output, input_options=("-f", "concat"), from_rgb=True,
                      scale_flags="neighbor" if sharp else "lanczos", max_seconds=once * loops)
    ffmpeg.run(ffmpeg_path, args, seconds=once * loops, progress=update)
    shutil.rmtree(folder, ignore_errors=True)


def save(made: list[Path], folder: Path, name: str) -> list[Path]:
    """Copy newly made videos into folder, in place of the shrine's old ones there.

    All or nothing: if something's in the way (e.g. Windows won't let go of an old video
    because it's open in a video player), OSError is raised and the folder is as it was.
    """
    folder.mkdir(parents=True, exist_ok=True)
    old = [path for path in folder.glob("*.mp4") if is_part_of(path.name, name)]
    aside: list[tuple[Path, Path]] = []
    placed: list[Path] = []
    try:
        for path in old:
            hidden = path.with_name(f".{path.name}.old")
            os.replace(path, hidden)
            aside.append((hidden, path))
        for video in made:
            placed.append(folder / video.name)
            shutil.copyfile(video, placed[-1])
    except BaseException:
        for path in placed:
            path.unlink(missing_ok=True)
        for hidden, path in aside:
            os.replace(hidden, path)
        raise
    for hidden, _ in aside:
        try:
            hidden.unlink()
        except OSError:
            pass  # hidden, so it's harmless
    return placed


# e.g. "IMG_1234.MOV" or "final.v2.mp4", but never part of another shrine ("smith.z.cat.mov")
_LEGACY_VIDEO = re.compile(
    r"(?![az]\.)(?!.*\.[az]\.)[^/]+\.(mp4|mov|avi|webm|mkv|ogv|mpe?g|m4v|wmv|gif|3gp|flv|mts|"
    r"m2ts|ts)", re.IGNORECASE)


def is_part_of(file_name: str, name: str) -> bool:
    """Whether a file on a TV (or in a folder) belongs to the shrine called name.

    Exact, so 'jo' and 'jo.a.smith' (or 'ann.lee' and 'ann.leeson') are never mixed up.
    """
    if file_name == f"{name}.a.mp4":
        return True
    prefix = f"{name}.z."
    if not (file_name.startswith(prefix) and file_name.endswith(".mp4")):
        return False
    video = file_name[len(prefix):-len(".mp4")]
    # The old control panel kept the original name: name.z.IMG_1234.MOV.mp4
    return bool(video) and ("." not in video or bool(_LEGACY_VIDEO.fullmatch(video)))
