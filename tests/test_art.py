import pytest
from fakes import FakeFilesConnection
from PIL import Image

from control_panel import art
from control_panel.art import ArtError, ArtFolder, ArtServer
from control_panel.config import TvsSettings


def make_server(art_dir="/mnt/ssd", **kwargs):
    connection = FakeFilesConnection(**kwargs)
    return ArtServer(connection, TvsSettings(art_dir=art_dir)), connection


ANN = ArtFolder(1, "/mnt/ssd/tv1/ann.lee", "ann.lee")


@pytest.mark.parametrize("label, expected", [
    ("Sunset at the Lake.JPG", "ann.lee.z.sunset_at_the_lake.jpg"),
    ("art/v2.final.png", "ann.lee.z.art-v2-final.png"),  # folders are kept together
    ("art.zip/v2.png", "ann.lee.z.v2.png"),
    ("作品.png", "ann.lee.z.art.png"),
    ("from a link", "ann.lee.z.from_a_link"),
    ("x" * 300 + ".png", "ann.lee.z." + "x" * art.MAX_PIECE_NAME + ".png"),
])
def test_stored_name(label, expected):
    assert art.stored_name("ann.lee", label, set()) == expected


def test_stored_names_are_unique():
    taken = {"ann.lee.z.sun.jpg"}
    assert art.stored_name("ann.lee", "SUN.jpg", taken) == "ann.lee.z.sun-2.jpg"
    assert art.stored_name("ann.lee", "sun.jpg", taken) == "ann.lee.z.sun-3.jpg"


def test_pieces_and_the_slideshow_order():
    files = ["ann.lee.z.pic_10.jpg", "Sunset.JPG", "ann.lee.a.title.png", "ann.lee.z.pic_2.jpg",
             "apple.png", "ann.lee.z.apple-2.png"]
    # Title card first, then by the piece's own name, however it was named.
    assert art.order(files, "ann.lee") == [
        "ann.lee.a.title.png", "apple.png", "ann.lee.z.apple-2.png", "ann.lee.z.pic_2.jpg",
        "ann.lee.z.pic_10.jpg", "Sunset.JPG"]
    assert art.piece("ann.lee.z.Sun Set.JPG", "ann.lee") == "sun_set.jpg"
    assert art.piece("Sun Set.jpg", "ann.lee") == "sun_set.jpg"


def test_folder_names_become_shrine_names():
    assert ArtFolder(1, "/x", "Ann.Lee").name == "ann.lee"
    assert ArtFolder(3, "/x", "Skills Canada 2023").name == "skills-canada-2023"


def test_the_drive_must_be_mounted():
    server, _ = make_server(mounted=False)
    with pytest.raises(ArtError, match="isn't on the external drive"):
        server.check_drive()


def test_check_drive_makes_a_missing_art_folder():
    server, connection = make_server(art_dir="/mnt/ssd/art/shrines")
    server.check_drive()
    assert {"/mnt/ssd/art", "/mnt/ssd/art/shrines"} <= connection.dirs
    assert not any(".control-panel-check" in path for path in connection.files)


def test_check_drive_explains_a_drive_it_cant_write_to():
    server, _ = make_server(locked={"/mnt/ssd"})
    with pytest.raises(ArtError, match="sudo chown -R pi: /mnt/ssd"):
        server.check_drive()
    server, _ = make_server(art_dir="/mnt/ssd/art", locked={"/mnt/ssd"})
    with pytest.raises(ArtError, match="sudo mkdir -p /mnt/ssd/art && sudo chown"):
        server.check_drive()
    server, _ = make_server(read_only=True)
    with pytest.raises(ArtError, match="read-only"):
        server.check_drive()
    server, _ = make_server(read_only=True, free_kb=0)
    with pytest.raises(ArtError, match="full"):
        server.check_drive()


def test_a_relative_art_dir_is_explained():
    server, _ = make_server(art_dir="mnt/ssd")
    with pytest.raises(ArtError, match="should start with /"):
        server.check_drive()


def test_video_names():
    paths = ["/a/ann.lee.z.clip.mov", "/a/ann.lee.z.clip.gif", "/a/ann.lee.z.dance.mp4",
             "/a/ann.lee.a.title.png"]
    assert art.video_name(paths[2], paths, "ann.lee") == "ann.lee.z.dance.mp4"
    # Two pieces with the same name get their types added, so their videos don't clash.
    assert art.video_name(paths[0], paths, "ann.lee") == "ann.lee.z.clip-mov.mp4"
    assert art.video_name(paths[1], paths, "ann.lee") == "ann.lee.z.clip-gif.mp4"
    assert art.video_name("/a/My Film.MOV", ["/a/My Film.MOV"], "ann.lee") == \
        "ann.lee.z.my_film.mp4"
    # hightower's names are kept exactly, so they match the videos already on the TVs.
    for kept in ["aidan.lanoville.z.Sonic_Barrel_Title", "cole.knudsen.z.hth.0.dev"]:
        name = kept.split(".z.")[0]
        assert art.video_name(f"/a/{kept}.mp4", [f"/a/{kept}.mp4"], name) == f"{kept}.mp4"
    assert art.video_name("/a/ben.b.z.Lego Animation.avi", [], "ben.b") == \
        "ben.b.z.Lego_Animation.mp4"


def test_stored_names_dont_share_a_piece_name():
    taken = {"ann.lee.z.clip.mov"}
    assert art.stored_name("ann.lee", "clip.gif", taken) == "ann.lee.z.clip-2.gif"


@pytest.mark.parametrize("name", ["a.jpe", "a.JFIF", "a.pjpeg", "a.avif", "a.png"])
def test_picture_types(name):
    assert art.is_picture(name) and art.is_art(name)


def test_copy_on_pi_files():
    server, connection = make_server()
    connection.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.clip.mp4", b"clip")
    server.copy("/mnt/ssd/tv1/ann.lee/ann.lee.z.clip.mp4",
                "/mnt/ssd/tv1/ann.lee/.videos/ann.lee.z.clip.mp4")
    assert connection.files["/mnt/ssd/tv1/ann.lee/.videos/ann.lee.z.clip.mp4"] == b"clip"


def test_shrines_and_find():
    server, connection = make_server()
    connection.add("/mnt/ssd/tv1/ann.lee/a.jpg")
    connection.add("/mnt/ssd/tv1/ann.leeson/a.jpg")
    connection.add("/mnt/ssd/tv1/.hidden/a.jpg")
    connection.add("/mnt/ssd/tv3/Skills Canada/a.jpg")
    assert [a.folder for a in server.shrines(1)] == ["ann.lee", "ann.leeson"]
    assert server.shrines(2) == []  # no folder yet
    assert [a.folder for a in server.find("ANN")] == ["ann.lee", "ann.leeson"]
    assert server.find("ann.lee", exact=True) == [ANN]
    skills = server.find("skills")[0]
    assert (skills.tv, skills.name) == (3, "skills-canada")


def test_art_files_and_videos():
    server, connection = make_server()
    connection.add("/mnt/ssd/tv1/ann.lee/a.jpg", b"aa")
    connection.add("/mnt/ssd/tv1/ann.lee/.videos/ann.lee.a.mp4", b"vvv")
    connection.add("/mnt/ssd/tv1/ann.lee/.videos/.made.json", b"{}")
    assert server.art_files(ANN) == {"/mnt/ssd/tv1/ann.lee/a.jpg": 2}
    assert server.videos(ANN) == {"ann.lee.a.mp4": 3}


def test_tidy_puts_hightowers_loose_files_where_they_go():
    server, connection = make_server()
    connection.add("/mnt/ssd/tv1/ann.lee/ann.lee.a.title.png")
    connection.add("/mnt/ssd/tv1/ann.lee.a.mp4", b"old slideshow")
    connection.add("/mnt/ssd/tv1/ann.lee.z.clip.mp4", b"clip")
    connection.add("/mnt/ssd/tv1/bo.ng.z.dance.mov", b"dance")  # no folder yet
    connection.add("/mnt/ssd/tv1/0_RubeGoldberg-Webb.mp4", b"demo")  # nobody's: it plays
    connection.add("/mnt/ssd/tv1/organize_images.py", b"hightower's script")
    connection.add("/mnt/ssd/tv1/odd.mov", b"?")
    said = server.tidy(1)
    assert connection.files["/mnt/ssd/tv1/ann.lee/ann.lee.z.clip.mp4"] == b"clip"
    assert connection.files["/mnt/ssd/tv1/bo.ng/bo.ng.z.dance.mov"] == b"dance"
    assert "/mnt/ssd/tv1/0_RubeGoldberg-Webb.mp4" in connection.files
    backup = connection.list_folder("/mnt/ssd/backup")[1]
    assert [f.endswith("-tv1-ann.lee.a.mp4") for f in backup] == [True]
    assert not any("organize_images" in s or "RubeGoldberg" in s for s in said)  # quietly
    assert any("odd.mov isn't in a shrine's folder" in s for s in said)
    assert server.tidy(1) == [said[-1]]  # only the one it can't place is left
    assert server.loose_videos(1) == {"0_RubeGoldberg-Webb.mp4": 4}


def test_a_locked_folder_is_explained(tmp_path):
    server, connection = make_server(locked={"/mnt/ssd/tv1/ann.lee"})
    connection.add("/mnt/ssd/tv1/ann.lee/a.jpg")
    picture = tmp_path / "b.jpg"
    picture.write_bytes(b"b")
    with pytest.raises(ArtError, match="won't let the app save in /mnt/ssd/tv1/ann.lee"):
        server.upload(picture, "/mnt/ssd/tv1/ann.lee/b.jpg")


def test_copying_files_both_ways(tmp_path):
    server, connection = make_server()
    picture = tmp_path / "new.png"
    Image.new("RGB", (4, 4), "blue").save(picture)
    server.upload(picture, "/mnt/ssd/tv1/ann.lee/.videos/x.png")  # its folders are made
    server.download("/mnt/ssd/tv1/ann.lee/.videos/x.png", tmp_path / "got.png")
    assert (tmp_path / "got.png").read_bytes() == picture.read_bytes()
    assert server.open("/mnt/ssd/tv1/ann.lee/.videos/x.png").read() == picture.read_bytes()
    server.write_json("/mnt/ssd/tv1/ann.lee/.videos/note.json", {"stills": 2})
    assert server.read_json("/mnt/ssd/tv1/ann.lee/.videos/note.json") == {"stills": 2}
    assert server.read_json("/mnt/ssd/nothing.json") == {}
    with pytest.raises(ArtError, match="Couldn't read missing.png on this computer"):
        server.upload(tmp_path / "missing.png", "/mnt/ssd/tv1/x.png")


def test_set_aside_and_delete():
    server, connection = make_server()
    connection.add("/mnt/ssd/tv1/ann.lee/a.jpg")
    connection.add("/mnt/ssd/tv1/ann.lee/b.jpg")
    aside = server.set_aside("/mnt/ssd/tv1/ann.lee/b.jpg", "b.jpg")
    assert aside.startswith("/mnt/ssd/backup/") and aside.endswith("-b.jpg")
    assert connection.files[aside] == b"art"
    server.delete("/mnt/ssd/tv1/ann.lee")
    assert not connection.exists("/mnt/ssd/tv1/ann.lee/a.jpg")
    assert not connection.exists("/mnt/ssd/tv1/ann.lee")
