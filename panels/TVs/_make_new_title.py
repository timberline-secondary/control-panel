import os
import inquirer
from .._utils import utils


def make_new_title():

    print("This function is for making a titlecard if there are no still images, only video\n")
    print("If the student has any images, then use make_slideshow instead.\n")
    print("For this to work, make sure you have Ubuntu fonts installed on the computer,\navailable through Google Fonts: https://fonts.google.com/specimen/Ubuntu \n")
    print("Inkscape also needs to be installed.\n")
    print("The file will be placed in the output folder.\n")

    username = utils.input_plus("Enter username for filename (firstname.lastname): ")
    if username == "q":
        return

    make_title_png(username, utils.OUTPUT_DIR)


def make_title_png(username, output_dir):

    fullname = utils.input_plus("Enter the name to appear in the title card: ")
    if fullname == "q":
        return
    grad_year = utils.input_plus("Grad Year: ")
    if grad_year == "q":
        return

    subject_list = [
        inquirer.List('subject',
                      message="What subject is the student in?",
                      choices=['Digital Art', 'Digital Photography', '3D Modelling & Animation', 'Custom subject:'],
                      ),
    ]
    subject = inquirer.prompt(subject_list)["subject"]
    # gets user to input a custom subject if they so choose
    if subject == "Custom subject:":
        subject = utils.input_styled("What is the subject? \n")

    filename = username + ".a.title"
    template = "_template.svg"
    source_file = os.path.join("panels", "TVs", template)
    temp_filepath_svg = os.path.join(utils.OUTPUT_DIR, f"{filename}.svg")
    filename_png = f"{filename}.png"
    out_filepath_png = os.path.join(output_dir, filename_png)

    # creates copy of template with the filename it will use
    os.system("copy {} {}".format(source_file, temp_filepath_svg))

    # open the file and replace templated sections
    try:
        # Open the file in read mode
        with open(temp_filepath_svg, 'r') as file:
            # Read the content of the file
            content = file.read()

        content = content.replace('FIRSTNAME LASTNAME', fullname)
        content = content.replace('YYYY', grad_year)
        content = content.replace('SUBJECT', subject.replace('&', '&amp;'))
        
        # Open the file in write mode to save the changes
        with open(temp_filepath_svg, 'w') as file:
            # Write the updated content back to the file
            file.write(content)

    except FileNotFoundError:
        print(f"Error: Could not write to {temp_filepath_svg}")
     
    # inkscape_command = f'{utils.INKSCAPE} -z -e {temp_filepath_png} -w 1920 -h 1080 {temp_filepath_svg}'
    inkscape_command = f'{utils.INKSCAPE} --export-filename={out_filepath_png} -w 1920 -h 1080 {temp_filepath_svg}'
    os.system(inkscape_command)


    if not os.path.isfile(out_filepath_png):
        utils.print_error(f"The title image '{filename_png}' was not added. Maybe inkscape isn't installed?")

    return out_filepath_png