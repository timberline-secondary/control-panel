from panels._utils import utils
import win32net
# from panels.User_Management import _utils as user_utils
from panels._utils.ssh import SSH

mime_types = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".avi": "video/x-msvideo",
    ".mpeg": "video/mpeg",
    ".mp4": "video/mp4",
    ".ogv": "video/ogg",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".svg": "image/svg+xml",
    ".gif": "image/gif"
}

TV_FILE_SERVER = "hightower"
TV_FILE_SERVER_USER = "pi-slideshow"
TV_FILE_SERVER_PW = "hackerberry"  # not secure, obvs.

TV_ROOT = "/home/{}".format(TV_FILE_SERVER_USER)

valid_tvs = ['1', '2', '3', '4']

TV_OFF_CMD = "echo standby 0 | cec-client -s -d 1"
TV_ON_CMD = "echo on 0 | cec-client -s -d 1"


def get_tv_containing_student(student_number):
    """ Search all pi-slideshow TV directories until directory with same number is found, return the TV # """
    ssh_connection = SSH(
        TV_FILE_SERVER, TV_FILE_SERVER_USER, TV_FILE_SERVER_PW)

    for tv in range(1, 5):
        filepath = "{}/tv{}/".format(TV_ROOT, tv)
        command = 'ls {}'.format(filepath)
        dir_contents = ssh_connection.send_cmd(
            command, print_stdout=False).split()
        if student_number in dir_contents:
            utils.print_success(
                "Found art for {} on TV# {}".format(student_number, tv))
            ssh_connection.close()
            return tv

    ssh_connection.close()
    return None


# reimplement getting user_utils.get_users_name() for windows
def get_display_name(username: str):
    try:
        user_info = win32net.NetGetUserInfo(
            win32net.NetGetAnyDCName(), username, 2)
        full_name = user_info["full_name"]
        return full_name
    except:
        return None


def guess_tv(username):
    # First, see if they already have art on a TV
    tv = get_tv_containing_student(username)

    # If they don't already have one, then guess based on their last name
    if tv is None:
        name = get_display_name(username)
        if name is None:
            utils.print_warning("I don't recognize the username {username}, it could be because of a typo, or they don't have a TV shrine start yet. Start a new Shrine with 'add_new_title'.")
            return 'q'

        # Last name A-L = 1, M-Z = 2
        # name variable will be fullname, split on spaces and take last element
        lastname = name.split()[-1]
        if lastname[0].lower() <= 'L':
            utils.print_warning(
                "Suggesting TV 1 because their last name, {}, is A-L".format(lastname))
            tv = 1
        else:
            utils.print_warning(
                "Suggesting TV 2 because their last name, {}, is M-Z".format(lastname))
            tv = 2

    return tv
