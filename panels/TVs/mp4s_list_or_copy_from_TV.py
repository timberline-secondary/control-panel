import inquirer
from ._mp4s_utils import choose_TV, choose_files, copy_from_TV


def mp4s_list_or_copy_from_TV():
    print("This function copies mp4 files to the \"output\" folder from a TV you specify.\n")

    tv = choose_TV()
    chosen = choose_files(tv)

    if len(chosen) == 0:
        print("No files selected.")
        return
    
    print(chosen)

    confirm = inquirer.prompt([inquirer.Confirm("copy", message="Copy these files?", default=True)])

    if confirm["copy"]:
        copy_from_TV(tv, chosen)
    else:
        print("Operation cancelled by user.")

    input("Hit Enter to continue. ")
