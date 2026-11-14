"""config.json ``logging`` key: disable tdb.log or set its level."""

from __future__ import annotations

import json
import logging

import pytest

from tdb import info, persist
from tdb.cli import _setup_logging
from tdb.persist import TdbConfig, log_level


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """Config and log dirs under tmp_path (neither created yet), with the
    root logger restored afterwards."""
    cfg_dir = tmp_path / "cfg"
    log_dir = tmp_path / "log"
    monkeypatch.setenv("TDB_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("TDB_LOG_DIR", str(log_dir))
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = []
    yield cfg_dir, log_dir
    for h in root.handlers:
        h.close()
    root.handlers = saved_handlers
    root.setLevel(saved_level)


def _fresh_setup_logging() -> None:
    # pytest attaches its capture handlers to the root logger for the call
    # phase, which would make basicConfig a no-op; start from a bare root
    # as a real `tdb` process does (the fixture restores it afterwards).
    logging.getLogger().handlers = []
    _setup_logging()


def _write_config(cfg_dir, text: str) -> None:
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(text)


def _emit_all_levels() -> None:
    lg = logging.getLogger("tdb.test_logging_config")
    lg.debug("dbg-msg")
    lg.info("info-msg")
    lg.warning("warn-msg")
    lg.error("err-msg")
    for h in logging.getLogger().handlers:
        h.flush()


# --- parsing ---------------------------------------------------------------


def test_default_is_debug():
    assert TdbConfig().logging == "DEBUG"
    assert log_level(TdbConfig().logging) == logging.DEBUG


@pytest.mark.parametrize(
    "value, level",
    [
        (False, None),
        (True, logging.DEBUG),
        ("DEBUG", logging.DEBUG),
        ("info", logging.INFO),
        ("WARN", logging.WARNING),
        ("Warning", logging.WARNING),
        ("ERROR", logging.ERROR),
        ("CRITICAL", logging.CRITICAL),
    ],
)
def test_valid_values(value, level):
    cfg = TdbConfig.from_dict({"logging": value})
    assert cfg.logging == value  # kept as written, so saves round-trip it
    assert log_level(cfg.logging) == level


@pytest.mark.parametrize("value", ["VERBOSE", "", 10, None, [], {"level": "INFO"}])
def test_invalid_values_fall_back_to_default(value):
    assert TdbConfig.from_dict({"logging": value}).logging == "DEBUG"


def test_round_trips_through_to_dict():
    cfg = TdbConfig.from_dict({"logging": False})
    assert cfg.to_dict()["logging"] is False
    assert TdbConfig.from_dict(json.loads(json.dumps(cfg.to_dict()))).logging is False


# --- _setup_logging ----------------------------------------------------------


def test_no_config_logs_everything(dirs):
    _cfg_dir, log_dir = dirs
    _fresh_setup_logging()
    _emit_all_levels()
    text = (log_dir / "tdb.log").read_text()
    assert "dbg-msg" in text and "err-msg" in text


def test_false_writes_no_log_and_creates_no_dir(dirs, tmp_path):
    cfg_dir, log_dir = dirs
    _write_config(cfg_dir, '{"logging": false}')
    _fresh_setup_logging()
    _emit_all_levels()
    assert not log_dir.exists()
    assert sorted(p.name for p in cfg_dir.iterdir()) == ["config.json"]
    # Silenced, not dropped onto stderr via logging.lastResort.
    assert logging.getLogger().level > logging.CRITICAL
    assert logging.getLogger().handlers


def test_false_keeps_stderr_clean(dirs, capsys):
    cfg_dir, _log_dir = dirs
    _write_config(cfg_dir, '{"logging": false}')
    _fresh_setup_logging()
    _emit_all_levels()
    assert capsys.readouterr().err == ""


def test_level_filters_records(dirs):
    cfg_dir, log_dir = dirs
    _write_config(cfg_dir, '{"logging": "warn"}')
    _fresh_setup_logging()
    _emit_all_levels()
    text = (log_dir / "tdb.log").read_text()
    assert "dbg-msg" not in text and "info-msg" not in text
    assert "warn-msg" in text and "err-msg" in text


def test_config_parse_error_reaches_log_not_terminal(dirs, capsys):
    cfg_dir, log_dir = dirs
    _write_config(cfg_dir, "{not json")
    _fresh_setup_logging()
    for h in logging.getLogger().handlers:
        h.flush()
    assert "Failed to load config" in (log_dir / "tdb.log").read_text()
    assert capsys.readouterr().err == ""


def test_unwritable_log_dir_falls_back_to_silence(dirs, tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("")
    # mkdir under a regular file fails with OSError on every platform.
    monkeypatch.setenv("TDB_LOG_DIR", str(blocker / "sub"))
    _fresh_setup_logging()
    assert logging.getLogger().level > logging.CRITICAL


# --- tdb --info ----------------------------------------------------------------


def test_info_log_row_notes_setting(tmp_path):
    path = tmp_path / "tdb.log"
    assert info._log_row(path).endswith("[not found]")
    assert info._log_row(path, True).endswith("[not found]")
    assert info._log_row(path, False).endswith("[not found] (disabled by config)")
    assert info._log_row(path, "warn").endswith("[not found] (level WARNING)")


def test_info_text_reads_logging_from_config(tmp_path, monkeypatch):
    monkeypatch.setenv("TDB_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("TDB_LOG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text('{"logging": false}')
    assert persist.load_config().logging is False
    assert "(disabled by config)" in info.info_text()
