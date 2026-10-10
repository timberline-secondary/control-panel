import pytest
from fakes import FakeFilesConnection
from PIL import Image

from control_panel.art import ArtError, ArtServer, Record, stored_name
from control_panel.config import TvsSettings
from control_panel.shrine import Title


def make_server(**kwargs):
    connection = FakeFilesConnection(**kwargs)
    return ArtServer(connection, TvsSettings()), connection


def test_record_round_trip():
    record = Record("ann.lee", Title("Ann Lee", "Digital Art", "2027"), ["0001-sun.jpg"],
                    ["ann.lee.z.cat.mp4"], [1, 4])
    again = Record.from_json(record.to_json())
    assert again == record
    assert Record.from_json(Record("skills", None).to_json()).title is None


@pytest.mark.parametrize("text", ["not json", '{"name": "x"}', '{"name": "x", "title": null, '
                                  '"pictures": ["../../etc/passwd"]}'])
def test_a_damaged_record_is_explained(text):
    with pytest.raises(ArtError, match="damaged"):
        Record.from_json(text)


@pytest.mark.parametrize("number, label, expected", [
    (1, "Sunset at the Lake.JPG", "0001-sunset_at_the_lake.jpg"),
    (12, "art/v2.final.png", "0012-v2-final.png"),
    (3, "作品.png", "0003-picture.png"),
    (4, "from a link", "0004-from_a_link"),
])
def test_stored_name(number, label, expected):
    assert stored_name(number, label) == expected


def test_the_drive_must_be_mounted():
    server, _ = make_server(mounted=False)
    with pytest.raises(ArtError, match="isn't on the external drive"):
        server.check_drive()


def test_check_drive_makes_the_art_folder():
    server, connection = make_server()
    server.check_drive()
    assert "/mnt/ssd/shrines" in connection.dirs


def test_check_drive_uses_sudo_if_the_drive_belongs_to_root():
    server, connection = make_server(root_owned=True)
    server.check_drive()
    assert ("sh -c 'mkdir -p /mnt/ssd/shrines && chown pi: /mnt/ssd/shrines'", True) in \
        connection.commands
    assert "/mnt/ssd/shrines" in connection.dirs


def picture(tmp_path, name, colour="red"):
    path = tmp_path / name
    Image.new("RGB", (20, 20), colour).save(path)
    return path


def test_keep_and_get_pictures(tmp_path):
    server, connection = make_server()
    server.check_drive()
    record = Record("ann.lee", Title("Ann Lee"))
    server.add_pictures(record, [(picture(tmp_path, "b.png"), "b.png"),
                                 (picture(tmp_path, "a.png", "blue"), "a.png")])
    server.add_pictures(record, [(picture(tmp_path, "c.png"), "new/c.png")])
    assert record.pictures == ["0001-b.png", "0002-a.png", "0003-c.png"]  # order kept
    server.save_record(record)
    assert server.names() == ["ann.lee"]
    got = server.get_pictures(server.load("ann.lee"), tmp_path / "got")
    assert [p.name for p in got] == record.pictures
    assert Image.open(got[1]).getpixel((0, 0)) == (0, 0, 255)


def test_videos_are_kept_and_listed(tmp_path):
    server, connection = make_server()
    server.check_drive()
    record = Record("ann.lee", None)
    videos = []
    for name in ["ann.lee.a.mp4", "ann.lee.z.cat.mp4"]:
        videos.append(tmp_path / name)
        videos[-1].write_bytes(b"video")
    server.add_videos(record, videos)
    assert record.videos == ["ann.lee.z.cat.mp4"]  # the slideshow isn't one of its videos
    assert "/mnt/ssd/shrines/ann.lee/videos/ann.lee.a.mp4" in connection.files


def test_a_shrine_only_counts_once_its_record_is_written(tmp_path):
    server, _ = make_server()
    server.check_drive()
    server.add_pictures(Record("half.done", None), [(picture(tmp_path, "a.png"), "a.png")])
    assert server.names() == []
    assert server.has_folder("half.done")


def test_set_aside_keeps_the_old_art(tmp_path):
    server, connection = make_server()
    server.check_drive()
    record = Record("ann.lee", None)
    server.add_pictures(record, [(picture(tmp_path, "a.png"), "a.png")])
    server.save_record(record)
    moved = server.set_aside("ann.lee")
    assert moved.startswith("/mnt/ssd/shrines/.replaced/ann.lee-")
    assert server.names() == [] and not server.has_folder("ann.lee")
    assert f"{moved}/pictures/0001-a.png" in connection.files


def test_a_full_drive_is_explained(tmp_path):
    server, _ = make_server(free_kb=10)
    server.check_drive()
    with pytest.raises(ArtError, match="full"):
        server.add_pictures(Record("ann.lee", None), [(picture(tmp_path, "a.png"), "a.png")])
