import contextlib
import datetime
import http.server
import subprocess
import threading
import zipfile

import pytest
from PIL import Image, ImageDraw

from control_panel import shrine
from control_panel.shrine import ANIMATION, PICTURE, VIDEO, ShrineError, Title


@pytest.mark.parametrize("text, space, expected", [
    ("Tyler.Couture", "_", "tyler.couture"),
    ("Tyler Couture", ".", "tyler.couture"),
    ("Zoë's Art  2", "_", "zoes_art_2"),
    ("..hidden", "_", "hidden"),  # a name starting with '.' would be hidden on the TV
    ("a. .b", "_", "a.b"),
    ("--x--", "_", "x"),
    ("名前", "_", ""),
])
def test_clean_name(text, space, expected):
    assert shrine.clean_name(text, space) == expected


@pytest.mark.parametrize("username, expected", [
    ("tyler.couture", "Tyler Couture"),
    ("mary-jane.smith", "Mary-Jane Smith"),
    ("ann", "Ann"),
])
def test_display_name(username, expected):
    assert shrine.display_name(username) == expected


@pytest.mark.parametrize("name, expected", [
    ("Tyler Couture", 1), ("ann.lee", 1), ("zed.lopez", 1),  # A-L
    ("Liam Moore", 2), ("a.mcdonald", 2), ("Émile Zola", 2),  # M-Z
    ("madonna", None), ("x.123", None),
])
def test_suggest_tv(name, expected):
    assert shrine.suggest_tv(name) == expected


def test_natural_order():
    names = ["pic 10.jpg", "pic 2.jpg", "Pic 1.jpg", "apple.png"]
    assert sorted(names, key=shrine.natural_key) == ["apple.png", "Pic 1.jpg", "pic 2.jpg",
                                                      "pic 10.jpg"]


@pytest.mark.parametrize("today, expected", [
    (datetime.date(2026, 10, 8), 2027), (datetime.date(2027, 3, 1), 2027),
    (datetime.date(2027, 6, 30), 2027), (datetime.date(2027, 8, 20), 2028),
])
def test_this_years_grads(today, expected):
    assert shrine.this_years_grads(today) == expected


def _colour_rows(card, colour):
    """The rows (y) that have pixels close to colour."""
    pixels = card.load()
    return {y for y in range(card.height) for x in range(0, card.width, 4)
            if all(abs(a - b) < 40 for a, b in zip(pixels[x, y], colour, strict=True))}


def test_title_card_layout():
    card = shrine.title_card(Title("Tyler Couture", "Digital Art", "2027"))
    assert card.size == (1920, 1080)
    cyan, white = _colour_rows(card, (0, 255, 232)), _colour_rows(card, (255, 255, 255))
    assert any(360 < y < 410 for y in cyan)  # "The Digital Art of"
    assert any(960 < y < 1010 for y in cyan)  # "Grad 2027"
    assert min(white) > 440 and max(white) < 650  # the name


def test_title_card_without_subject_or_year():
    card = shrine.title_card(Title("Skills Canada 2026"))
    assert not _colour_rows(card, (0, 255, 232))
    assert _colour_rows(card, (255, 255, 255))


def test_long_names_fit_on_the_card():
    card = shrine.title_card(Title("Maximiliana Wolfeschlegelsteinhausen-Bergerdorff"))
    left, _, right, _ = card.getbbox()
    assert left >= 55 and right <= 1920 - 55


def make_media(folder):
    folder.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), "red").save(folder / "pic 10.jpg")
    Image.new("RGB", (40, 30), "blue").save(folder / "pic 2.png", format="JPEG")  # misnamed
    frames = [Image.new("RGB", (16, 16), c) for c in ("red", "green", "blue")]
    frames[0].save(folder / "dance.gif", save_all=True, append_images=frames[1:], duration=200)
    (folder / "notes.txt").write_text("not art")
    (folder / "drawing.svg").write_text("<svg/>")
    (folder / ".hidden.jpg").write_bytes(b"")
    (folder / "Thumbs.db").write_bytes(b"")
    (folder / "sub").mkdir(exist_ok=True)
    Image.new("RGB", (40, 30), "green").save(folder / "sub" / "pic 1.jpg")


def test_classify_goes_by_whats_in_the_file(tmp_path):
    make_media(tmp_path)
    no_ffmpeg = tmp_path / "ffmpeg-is-not-needed-for-pictures"
    assert shrine.classify(no_ffmpeg, tmp_path / "pic 2.png") == PICTURE
    assert shrine.classify(no_ffmpeg, tmp_path / "dance.gif") == ANIMATION
    with pytest.raises(ShrineError, match="Export it as a PNG"):
        shrine.classify(no_ffmpeg, tmp_path / "drawing.svg")


def test_gather_a_folder(tmp_path, ffmpeg_path):
    make_media(tmp_path / "art")
    found, skipped = shrine.gather(ffmpeg_path, tmp_path / "art", tmp_path / "work")
    assert [(m.label, m.kind) for m in found] == [
        ("dance.gif", ANIMATION), ("pic 2.png", PICTURE), ("pic 10.jpg", PICTURE),
        ("sub/pic 1.jpg", PICTURE),
    ]
    assert {s.label for s in skipped} == {"drawing.svg", "notes.txt"}


def test_gather_a_zip_file(tmp_path, ffmpeg_path):
    make_media(tmp_path / "art")
    with zipfile.ZipFile(tmp_path / "art.zip", "w") as archive:
        for path in sorted((tmp_path / "art").rglob("*.jpg")):
            archive.write(path, path.relative_to(tmp_path).as_posix())
    (tmp_path / "work").mkdir()
    found, _ = shrine.gather(ffmpeg_path, tmp_path / "art.zip", tmp_path / "work")
    assert [m.label for m in found] == ["art.zip/art/pic 10.jpg", "art.zip/art/sub/pic 1.jpg"]


def test_gather_finds_videos(tmp_path, ffmpeg_path):
    video = tmp_path / "clip.webm"
    subprocess.run([str(ffmpeg_path), "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=64x48:rate=10:duration=1", str(video)], check=True)
    assert shrine.classify(ffmpeg_path, video) == VIDEO


def test_prepare_picture_turns_and_letterboxes(tmp_path):
    photo = Image.new("RGB", (300, 200), "blue")
    ImageDraw.Draw(photo).rectangle([0, 0, 30, 30], fill="yellow")  # top left
    exif = photo.getexif()
    exif[0x0112] = 6  # the camera was turned: show it rotated 90 degrees clockwise
    photo.save(tmp_path / "photo.jpg", exif=exif)
    shrine.prepare_picture(tmp_path / "photo.jpg", tmp_path / "slide.png")
    slide = Image.open(tmp_path / "slide.png")
    assert slide.size == (1920, 1080) and slide.mode == "RGB"
    assert slide.getbbox() == (600, 0, 1320, 1080)  # upright and tall, with black sides
    assert slide.getpixel((1300, 20))[:2] > (200, 200)  # yellow is now top right


def test_prepare_picture_puts_transparency_on_black(tmp_path):
    Image.new("RGBA", (100, 100), (255, 255, 255, 0)).save(tmp_path / "clear.png")
    shrine.prepare_picture(tmp_path / "clear.png", tmp_path / "slide.png")
    assert Image.open(tmp_path / "slide.png").getbbox() is None  # all black


def test_slideshow_timeline():
    args = shrine.slideshow_args(["0.png", "1.png", "2.png"], "out.mp4")
    graph = args[args.index("-filter_complex") + 1]
    assert "xfade=transition=fade:duration=1:offset=8[x1]" in graph
    assert "xfade=transition=fade:duration=1:offset=16[x2]" in graph
    assert "fade=t=out:st=24:d=1" in graph
    assert args[args.index("-t", args.index("-map")) + 1] == "25"
    assert shrine.slideshow_seconds(1) == 9  # one slide still ends (the old one never did)


def test_animation_frames_and_playlist(tmp_path):
    frames = [Image.new("RGBA", (8, 8), c) for c in ("red", "green", (0, 0, 255, 0))]
    frames[0].save(tmp_path / "a.webp", save_all=True, append_images=frames[1:],
                   duration=[200, 0, 300])
    saved, size = shrine.animation_frames(tmp_path / "a.webp", tmp_path / "frames")
    assert size == (8, 8)
    assert [round(seconds, 3) for _, seconds in saved] == [0.2, 0.1, 0.3]  # 0 ms -> 100 ms
    assert Image.open(saved[2][0]).getpixel((0, 0)) == (0, 0, 0)  # transparent -> black
    shrine.write_playlist(saved, 2, tmp_path / "frames.txt")
    lines = (tmp_path / "frames.txt").read_text().splitlines()
    assert lines[0] == "ffconcat version 1.0"
    assert lines.count("file '00000.png'") == 2
    assert lines[-1] == "file '00002.png'"


def test_video_file_names():
    taken = set()
    assert shrine.video_file_name("ann.lee", "Videos/My Film.MOV", taken) == \
        "ann.lee.z.my_film.mp4"
    assert shrine.video_file_name("ann.lee", "my film.mp4", taken) == "ann.lee.z.my_film-2.mp4"
    assert shrine.video_file_name("ann.lee", "???.gif", taken) == "ann.lee.z.video.mp4"


def test_is_part_of():
    assert shrine.is_part_of("ann.lee.a.mp4", "ann.lee")
    assert shrine.is_part_of("ann.lee.z.cat.mp4", "ann.lee")
    assert not shrine.is_part_of("ann.leeson.a.mp4", "ann.lee")
    assert not shrine.is_part_of("ann.lee.a.mp4.part", "ann.lee")
    assert not shrine.is_part_of("ann.lee.a.mp4", "ann")


def test_remove_old(tmp_path):
    for name in ["ann.lee.a.mp4", "ann.lee.z.cat.mp4", "ann.leeson.a.mp4", "notes.txt"]:
        (tmp_path / name).write_bytes(b"")
    shrine.remove_old(tmp_path, "ann.lee")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ann.leeson.a.mp4", "notes.txt"]


@pytest.mark.parametrize("url, expected", [
    ("https://drive.google.com/file/d/abc_123/view?usp=sharing",
     "https://drive.google.com/uc?export=download&id=abc_123"),
    ("https://drive.google.com/open?id=xyz", "https://drive.google.com/uc?export=download&id=xyz"),
    ("https://www.dropbox.com/s/q1/art.png?dl=0", "https://www.dropbox.com/s/q1/art.png?dl=1"),
    ("https://school-my.sharepoint.com/:i:/g/personal/x/Eab?e=1",
     "https://school-my.sharepoint.com/:i:/g/personal/x/Eab?e=1&download=1"),
    ("https://example.com/cat.png", "https://example.com/cat.png"),
])
def test_direct_link(url, expected):
    assert shrine.direct_link(url) == expected


@pytest.fixture
def web_server():
    """Serves {path: (status, content type, body, extra headers)}."""
    routes = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status, content_type, body, headers = routes[self.path]
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield routes, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_download_link(web_server, tmp_path):
    routes, base = web_server
    routes["/art/cat%20pic.png"] = (200, "image/png", b"PNG", {})
    routes["/dl"] = (200, "application/octet-stream", b"JPG",
                     {"Content-Disposition": "attachment; filename=\"../../evil.jpg\""})
    routes["/page"] = (200, "text/html", b"<html>Sign in</html>", {})
    routes["/private"] = (403, "text/plain", b"no", {})
    assert shrine.download_link(f"{base}/art/cat%20pic.png", tmp_path).name == "cat pic.png"
    saved = shrine.download_link(f"{base}/dl", tmp_path)
    assert saved == tmp_path / "evil.jpg" and saved.read_bytes() == b"JPG"  # stays in tmp_path
    assert shrine.download_link(f"{base}/dl", tmp_path).name == "evil-2.jpg"
    with pytest.raises(ShrineError, match="opens a web page"):
        shrine.download_link(f"{base}/page", tmp_path)
    with pytest.raises(ShrineError, match="opens a web page"):
        shrine.download_link(f"{base}/private", tmp_path)


def test_sign_in_links_are_explained():
    assert "school sign-in" in shrine._web_page_message("https://x-my.sharepoint.com/a")
    assert "Google Drive" in shrine._web_page_message("https://drive.google.com/file/d/1")


@contextlib.contextmanager
def no_progress(label):
    yield lambda fraction: None


def _duration(ffmpeg_path, video):
    from control_panel import ffmpeg
    return ffmpeg.probe(ffmpeg_path, video).duration


def test_make_a_shrine(tmp_path, ffmpeg_path):
    make_media(tmp_path / "art")
    (tmp_path / "art" / "broken.jpg").write_bytes(b"\xff\xd8\xff\xe0 not really a jpeg")
    media, _ = shrine.gather(ffmpeg_path, tmp_path / "art", tmp_path / "work")
    media.append(shrine.Media(tmp_path / "art" / "broken.jpg", PICTURE, "broken.jpg"))
    made, problems = shrine.make(ffmpeg_path, "ann.lee", Title("Ann Lee", "Digital Art", "2027"),
                                 media, tmp_path / "work", no_progress)
    assert [p.name for p in made] == ["ann.lee.a.mp4", "ann.lee.z.dance.mp4"]
    assert [p.label for p in problems] == ["broken.jpg"]
    assert _duration(ffmpeg_path, made[0]) == pytest.approx(8 * 4 + 1, abs=0.1)  # title + 3
    assert _duration(ffmpeg_path, made[1]) == pytest.approx(5.4, abs=0.1)  # 0.6 s, 9 times


def test_make_a_title_only_shrine(tmp_path, ffmpeg_path):
    made, problems = shrine.make(ffmpeg_path, "ann.lee", Title("Ann Lee"), [], tmp_path,
                                 no_progress)
    assert [p.name for p in made] == ["ann.lee.a.mp4"] and not problems
    assert _duration(ffmpeg_path, made[0]) == pytest.approx(9, abs=0.1)
