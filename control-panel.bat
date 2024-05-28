@echo off

ECHO:
ECHO Python 3.11 is required. Install it from Python.org if not already installed
ECHO:

SET CURDIR=%~dp0
cd %CURDIR%

@REM temporary variable for python path until python is installed in PATH, is version dependant (Python311)
@REM SET git="C:\Users\%USERNAME%\AppData\Local\GithubDesktop\app-3.3.6\resources\app\git\cmd\git.exe"
SET git="git"
@REM SET python="C:\Program Files\Python311\python.exe"
SET python=py -3.11


ECHO:
@REM ### update git ### ------------>>>
ECHO ### PULLING FROM GITHUB ###
git pull

IF NOT %errorlevel% EQU 0 (
    ECHO ^[ERROR^]^: Something went wrong while pulling from git.
    PAUSE

    exit /b 1
)
ECHO ### PULLED FROM GITHUB ###


ECHO:
@REM ### install python env ### ------------>>>
IF NOT EXIST %CURDIR%\env (
    ECHO ### INSTALLING PYTHON ENVIRONMENT ###
    ECHO Creating ./env ...
    %python% -m venv %CURDIR%\env
    IF NOT %errorlevel% EQU 0 (
        ECHO ^[ERROR^]^: something went wrong while running installing python venv.
        PAUSE
        
        exit /b 1
    )

    ECHO ### INSTALLED PYTHON ENVIRONMENT ###
)


ECHO:
@REM activate venv
ECHO ### ACTIVATING PYTHON ENVIRONMENT ###
ECHO Activating python virtual environment ...
CALL %CURDIR%\env\Scripts\activate

IF NOT %errorlevel% EQU 0 (
    ECHO ^[ERROR^]^: Something went wrong while activating venv.
    PAUSE
    
    exit /b 1
)
ECHO ### ACTIVATED PYTHON ENVIRONMENT ###


ECHO:
@REM ### update pip deps ### ------------>>>
ECHO ### UPDATING PIP DEPENDENCIES ###
echo %python%
%python% -m pip install -r requirements.txt

IF NOT %errorlevel% EQU 0 (
    ECHO ^[ERROR^]^: Something went wrong while updating pip deps.
    PAUSE

    exit /b 1
)
ECHO ### DEPENDENCIES UPDATED ###

@REM ### run the python script ### ------------>>>
%python% %CURDIR%\main.py




@REM ### CLEANUP SECTION ### ------------>>>
@REM cd "C:\Users\%USERNAME%\"

CALL %CURDIR%\env\Scripts\deactivate
IF NOT %errorlevel% EQU 0 (
    ECHO ^[ERROR^]^: Something went wrong while deactivating venv.
    PAUSE
    
    exit /b 1
)
