import os
from pathlib import Path

import pytest
from pytest import MonkeyPatch

# Importing litellm in its default DEV mode runs load_dotenv(), injecting the repo
# .env into os.environ for the whole pytest process during collection; any test
# asserting Settings defaults then sees local overrides instead. PRODUCTION mode
# disables that import-time side effect. Assigned unconditionally: an inherited
# LITELLM_MODE=DEV from the shell would silently defeat the guard.
os.environ["LITELLM_MODE"] = "PRODUCTION"


@pytest.fixture(autouse=True)
def _isolate_repo_env_file(  # pyright: ignore[reportUnusedFunction] -- autouse fixture
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    # Settings resolves env_file=".env" against the working directory, so a repo-root
    # pytest run would fold the developer's .env into every bare Settings() in the
    # suite. Subprocess-spawning tests pin their own cwd and are unaffected.
    monkeypatch.chdir(tmp_path)
