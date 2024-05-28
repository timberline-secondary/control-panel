import os
import shutil
from .._utils import utils, movie_maker_fade
from ._make_new_title import make_title_png

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
    ".gif": "image/gif",
    ".mkv": "video/x-matroska"
}

def is_video(file_extension):
    """ file should already have mimetype checked at this point! """
    video_extensions = [".avi", ".mpeg", ".mp4", ".ogv", ".webm", ".mkv"]
    return file_extension.lower() in video_extensions

def make_slideshow(username=None):
    print("This function takes a folder of media (without a title card) and creates mp4 files to be pushed to the TV and OneDrive.\n")
    print("For this to work, make sure you have Ubuntu fonts installed on the computer,\navailable through Google Fonts: https://fonts.google.com/specimen/Ubuntu \n")
    print("Inkscape also needs to be installed.\n")
    print("The cleaned up images and mp4s will be placed in the \"output\" folder.\n ")
    print("Copy the mp4s and the folder of cleaned images to the Hackerspace OneDrive for backup storage.\n")
    print("pi-tv1 original student work A-L\npi-tv2 original student work M-Z\npi-tv3 work from tutorials and Skills Canada\npi-tv4 everything for hallway\n")

    media_folder = utils.input_styled("\nDrop folder of media files into window, or enter q to quit: \n")
    print(media_folder)
    if media_folder.lower().strip() == "q":
        return 
    
    media_folder_path = media_folder.replace('"', '') #strip quotes
    if not os.path.isdir(media_folder_path):
        print("Not a valid directory")
        return
    username = utils.input_styled("Enter username (firstname.lastname) for folder filenames, or enter q to quit: \n").lower().strip()
    if username == 'q':
        return
    
    media_out_folder_path = os.path.join(utils.OUTPUT_DIR, username)
    if os.path.exists(media_out_folder_path):
        print("There is already a folder in the output folder with the name {}. Move it out of the output folder.".format(username))
        return

    # DO stuff with user input
    os.mkdir(media_out_folder_path)

    title_path = make_title_png(username, media_out_folder_path)
    
    for dir_entry in os.scandir(media_folder_path):

        print(dir_entry.name)

        filepath = os.path.join(media_folder_path, dir_entry.name)

        print(f"Processing {filepath}\n".format)

        filepath_base, ext = os.path.splitext(filepath)

        expected_mime_type = None  # Reset
        try:
            expected_mime_type = mime_types[ext.lower()]
        except KeyError:
            # un supported extension
            expected_mime_type = None
            print(f"{filepath} mime type not supported.\n".format)
            continue

        # If correct mime type verify integrity of media file
        if utils.verify_mimetype(filepath, expected_mime_type):
            success, fixed_url, ext = utils.verify_image_integrity(filepath, expected_mime_type, ext)
            print(success)
            print(fixed_url)
            print(ext)
            if success:
                if is_video(ext):
                    print(f"{fixed_url} is video\n")
                    video_file_path = os.path.join(utils.OUTPUT_DIR, f"{username}.z.{dir_entry.name}.mp4".replace(" ", "_"))
                    print(f"Moving to {video_file_path}")
                    shutil.move(fixed_url, video_file_path)
                else:
                    image_file_path = os.path.join(media_out_folder_path, f"{username}.z.{dir_entry.name}".replace(" ", "_"))
                    shutil.move(fixed_url, image_file_path)
            else:
                print("Error: problem with media could not be fixed. Skipping {}\n".format(filepath))
                continue
        else:
            print("Unexpected file extension for {}".format(media_folder_path))
            continue

    output_name = os.path.join(utils.OUTPUT_DIR, username + ".a.mp4")
    print(output_name)
    movie_maker_fade.movie_maker_fade(images_directory=media_out_folder_path, output_file=output_name)

    os.remove(title_path)



