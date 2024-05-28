
import inquirer
from ._mp4s_utils import choose_TV, choose_files, delete_from_TV

def mp4s_list_or_delete_from_TV():
    print("This function delets mp4 files from a TV you specify.\n")

    tv = choose_TV()

    chosen = choose_files(tv)

    if len(chosen) == 0:
        print("No files selected.")
        return
    
    print(chosen)

    confirm = inquirer.prompt([inquirer.Confirm("delete", message="Delete these files?", default=False)])

    if confirm["delete"]:
        delete_from_TV(tv, chosen)
    else:
        print("Operation cancelled by user.")

    input("Hit Enter to continue. ")