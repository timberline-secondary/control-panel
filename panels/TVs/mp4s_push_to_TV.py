import inquirer
import os
from ._mp4s_utils import choose_TV, push_to_TV
from .._utils import utils


def mp4s_push_to_TV():
    print("This function pushes any mp4 files in the \"output\" folder to a TV you specify.\n")
    
    mp4_files = [f for f in os.listdir(utils.OUTPUT_DIR) if f.endswith(".mp4")]

    if len(mp4_files) == 0:
        print("there are no mp4s in the output folder.")
        return

    tv = choose_TV()

    if len(mp4_files) > 1:
        chosen = inquirer.prompt(
            [
                inquirer.Checkbox('chosen_files',
                        message="Which files to push? use spacebar to select. arrows to move. enter when done.",
                        choices=mp4_files,
                        ),
            ]   
        )
        files = chosen['chosen_files']
    else:
        files = mp4_files

    print(files)

    confirm = inquirer.prompt([inquirer.Confirm("push", message=f"Push these files to {tv}?", default=True)])

    if confirm["push"]:
        push_to_TV(tv, files)
    else:
        print("Operation cancelled by user.")

    input("Hit Enter to continue. ")
