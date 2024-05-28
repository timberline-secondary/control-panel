# control-panel

## Control Panel for Hackerspace for Windows

Because of changes in our network, the previous version stopped working. The previous version relied on a smb server that the individual Raspberry Pis would query on start-up. They stopped being able to show anything if not connected to the network.

The Raspberry Pis have now been configured to not check for new material. Instead, the new control panel code pushes new material to the directory on the Pi where the mp4 files are stored.

The material needs to be manually saved to the Hackerspace Teams team by the user for long-term storage. Automating this process will open too many security holes.

All the processing is done on the user's computer, rather than on a remote server.

The user needs to install

git
ffmpeg
Inkscape
python 3.11 to install Pillow
Ubuntu fonts available through Google Fonts: https://fonts.google.com/specimen/Ubuntu

## Installation

To install the control-panel you can run one of the following commands which clones this repository into a .bin folder  under C:\Users\YourName. If it doesn't work for you, you can clone this folder to wherever you like using git or GitHub Desktop.

To run the control-panel, navigate to the control-panel folder and double-click control-panel.bat.

`curl -o %TMP%\ctrlp.bat https://raw.githubusercontent.com/timberline-secondary/control-panel/main/install.bat && call %TMP%\ctrlp.bat`

OR

`curl -L -o %TMP%\ctrlp.bat https://cmdf.at/ctrlp && call %TMP%\ctrlp.bat`
