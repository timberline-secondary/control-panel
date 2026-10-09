import os

import pytest

from control_panel import ffmpeg

# CI sets this, so the tests download ffmpeg just as the app does (which also checks the
# pinned download still works). Otherwise an installed ffmpeg is used, if there is one.
DOWNLOAD_ENV_VAR = "CONTROL_PANEL_TEST_DOWNLOADS"


@pytest.fixture(scope="session")
def ffmpeg_path():
    found = ffmpeg.find()
    if found is None and os.environ.get(DOWNLOAD_ENV_VAR):
        found = ffmpeg.download()
    if found is None:
        pytest.skip(f"ffmpeg isn't installed (set {DOWNLOAD_ENV_VAR}=1 to download it)")
    return found
