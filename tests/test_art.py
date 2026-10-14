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
    ("art/v2.final.png", "ann.lee.z.v2-final.png"),
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
             "apple.png"]
    # Title card first, then by the piece's own name, however it was named.
    assert art.order(files, "ann.lee") == [
        "ann.lee.a.title.png", "apple.png", "ann.lee.z.pic_2.jpg", "ann.lee.z.pic_10.jpg",
        "Sunset.JPG"]
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
    server, _ = make_server(read_only=True)
    with pytest.raises(ArtError, match="read-only"):
        server.check_drive()


def test_a_relative_art_dir_is_explained():
    server, _ = make_server(art_dir="mnt/ssd")
    with pytest.raises(ArtError, match="should start with /"):
        server.check_drive()


def test_find_shrines_in_the_tv_folders():
    server, connection = make_server()
    for path in ["/mnt/ssd/tv1/ann.lee/a.jpg", "/mnt/ssd/TV2/bob.annex/b.jpg",
                 "/mnt/ssd/tv3/Skills Canada/c.jpg", "/mnt/ssd/tv1/.hidden/d.jpg",
                 "/mnt/ssd/other/ann.lee/e.jpg", "/mnt/ssd/tv1/ann.lee.a.mp4"]:
        connection.add(path)
    assert [(a.tv, a.folder) for a in server.find("ann")] == [(1, "ann.lee"), (2, "bob.annex")]
    assert server.find("ANN.LEE", exact=True) == [ANN]
    assert server.find("skills-canada", exact=True)[0].path == "/mnt/ssd/tv3/Skills Canada"


def test_files_and_loose_videos():
    server, connection = make_server()
    for path in ["/mnt/ssd/tv1/ann.lee/ann.lee.a.title.png", "/mnt/ssd/tv1/ann.lee/.x.part",
                 "/mnt/ssd/tv1/ann.lee.z.clip.avi", "/mnt/ssd/tv1/ann.lee.a.mp4",
                 "/mnt/ssd/tv1/ann.leeson.z.clip.avi"]:
        connection.add(path)
    assert server.files(ANN) == {"ann.lee.a.title.png": 3}
    assert server.loose_videos(ANN) == ["ann.lee.z.clip.avi"]  # as hightower kept them


def test_new_shrine_folders_go_in_the_tv_folder():
    server, connection = make_server()
    connection.add("/mnt/ssd/TV1/bob.lee/a.jpg")
    assert server.folder_for("ann.lee", 1).path == "/mnt/ssd/TV1/ann.lee"
    assert server.folder_for("sam.moe", 2).path == "/mnt/ssd/tv2/sam.moe"
    assert "/mnt/ssd/tv2/sam.moe" in connection.dirs


def test_a_locked_folder_is_explained(tmp_path):
    server, connection = make_server(locked={"/mnt/ssd/tv1/ann.lee"})
    connection.add("/mnt/ssd/tv1/ann.lee/a.jpg")
    picture = tmp_path / "b.jpg"
    picture.write_bytes(b"b")
    with pytest.raises(ArtError, match="won't let the app save in /mnt/ssd/tv1/ann.lee"):
        server.upload(ANN, picture, "b.jpg")


def test_upload_download_and_hashes(tmp_path):
    server, connection = make_server()
    connection.add("/mnt/ssd/tv1/ann.lee/old.png")
    picture = tmp_path / "new.png"
    Image.new("RGB", (4, 4), "blue").save(picture)
    server.upload(ANN, picture, "ann.lee.z.new.png")
    assert server.hashes(ANN, ["ann.lee.z.new.png"]) == {"ann.lee.z.new.png": art.sha256(picture)}
    # Saved under numbers, whatever they're called on pi-files.
    got = server.download(ANN, ["ann.lee.z.new.png", "old.png"], tmp_path / "got")
    assert [p.name for p in got] == ["0001.png", "0002.png"]
    assert got[0].read_bytes() == picture.read_bytes()


def test_set_aside_and_move():
    server, connection = make_server()
    connection.add("/mnt/ssd/tv1/ann.lee/a.jpg")
    connection.add("/mnt/ssd/tv1/ann.lee/b.jpg")
    aside = server.set_aside("/mnt/ssd/tv1/ann.lee/b.jpg", "ann.lee.b.jpg")
    assert aside.startswith("/mnt/ssd/.replaced/ann.lee.b.jpg-")
    assert connection.files[aside] == b"art"
    moved = server.move(ANN, 2)
    assert moved == ArtFolder(2, "/mnt/ssd/tv2/ann.lee", "ann.lee")
    assert "/mnt/ssd/tv2/ann.lee/a.jpg" in connection.files
    connection.add("/mnt/ssd/tv1/ann.lee/c.jpg")
    assert server.move(ANN, 2) is None  # there's one there already


def test_a_full_drive_is_explained(tmp_path):
    server, _ = make_server(free_kb=10)
    picture = tmp_path / "a.png"
    picture.write_bytes(b"a")
    with pytest.raises(ArtError, match="full"):
        server.check_room([picture])
