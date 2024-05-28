import os
import paramiko
import inquirer
from .._utils import utils

username = 'pi'
password = 'hackerberry'
domain = '.hackerspace.tbl'
media_dir = '/home/pi/rs_media/'

ssh = None

def connect_sftp(tv):
    global ssh
    if ssh is None:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # Automatically add host keys (not secure for production)
    ssh.connect(hostname=tv + domain, username=username, password=password)
    return ssh.open_sftp()


def choose_TV():
    tv_list = [
        inquirer.List('tv',
                      message="Which TV?",
                      choices=[("TV1 for original artwork, students A-L", "pi-tv1"),
                               ("TV2 for original artwork, students M-Z", "pi-tv2"),
                               ("TV3 for demonstration artwork or Skills Cananda", "pi-tv3"),
                               ("TV4 for hallway", "pi-tv4"),
                               ],
                               carousel=True
                      ),
    ]
    return inquirer.prompt(tv_list)["tv"]


def choose_files(tv):
    sftp = connect_sftp(tv)
    file_list = sorted(sftp.listdir(media_dir))
    sftp.close()
    ssh.close()

    chosen = inquirer.prompt(
        [
            inquirer.Checkbox('chosen_files',
                    message="Which files? use spacebar to select. arrows to move. enter when done.",
                    choices=file_list,
                    ),
        ]   

    )

    return chosen['chosen_files']


def copy_from_TV(tv, file_list):
    sftp = connect_sftp(tv)

    for file in file_list:
        print(f'Trying to copy {file}')
        try:
            outfile = os.path.join(utils.OUTPUT_DIR, file)
            remote_file = os.path.join(media_dir, file)
            print(f'copying {remote_file} to {outfile}')
            sftp.get(remote_file, outfile)
        except Exception as e:
            print(f"Failed to copy {file}\nException {e}")
        else:
            print(f"No errors detected in copying {file}")

    sftp.close()
    ssh.close()


def delete_from_TV(tv, file_list):
    sftp = sftp = connect_sftp(tv)

    for file in file_list:
        print(f'Trying to delete {file}')
        try:
            remote_file = os.path.join(media_dir, file)
            sftp.remove(remote_file)
        except Exception as e:
            print(f"Failed to delete {file}\nException: {e}")
        else:
            print(f"No errors detected in deleting {file}")

    sftp.close()
    ssh.close()


def push_to_TV(tv, file_list):
    sftp = connect_sftp(tv)

    for file in file_list:
        print(f'Trying to push {file}')
        try:
            outfile = os.path.join(utils.OUTPUT_DIR, file)
            remote_file = os.path.join(media_dir, file)
            print(f'copying {outfile} to {remote_file}')
            sftp.put(outfile, remote_file)
        except Exception as e:
            print(f"Failed to push {file}\nException {e}")
        else:
            print(f"No errors detected in pushing {file}")

    sftp.close()
    ssh.close()

    