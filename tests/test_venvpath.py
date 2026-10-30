import importlib.util
import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("venvpath", REPO / "scripts" / "venvpath.py")
venvpath = importlib.util.module_from_spec(spec)
spec.loader.exec_module(venvpath)


def test_the_tag_names_the_os_the_architecture_and_the_checkout(tmp_path):
    assert re.fullmatch(r"(linux|darwin)-[A-Za-z0-9_]+-[0-9a-f]{6}", venvpath.env_tag(tmp_path))


def test_different_paths_get_different_venvs(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert venvpath.venv_dir(tmp_path / "a").name != venvpath.venv_dir(tmp_path / "b").name


def test_a_symlinked_checkout_resolves_to_the_same_venv(tmp_path):
    (tmp_path / "real").mkdir()
    os.symlink(tmp_path / "real", tmp_path / "link")
    assert venvpath.venv_dir(tmp_path / "link") == venvpath.venv_dir(tmp_path / "real")


def test_the_venv_is_in_the_local_cache_not_in_the_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    directory = venvpath.venv_dir(tmp_path / "checkout")
    assert directory.parent == tmp_path / "cache" / "apple" and directory.name.startswith("venv-")


def test_without_xdg_cache_home_it_is_under_dot_cache_in_the_home_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert venvpath.venv_dir(tmp_path / "checkout").parent == tmp_path / ".cache" / "apple"
