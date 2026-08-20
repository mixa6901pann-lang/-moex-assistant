"""Static check: main.py has a singleton lock that prevents two instances.

The actual lock test requires fcntl (Linux only). The live test was
done manually on the server on 2026-08-19: a second `python main.py`
while supervisord ran the first exited with code 1 and the message
"Another moex instance already holds the singleton lock."

This test guards against accidental removal of the lock by regressing
the path. It does not import main.py (which would pull Tinkoff and
other heavy deps on import).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MAIN_PY = ROOT / "main.py"


def test_main_py_has_lock_acquire_call():
    """main.py must call fcntl.flock with LOCK_EX|LOCK_NB."""
    text = MAIN_PY.read_text(encoding="utf-8")
    assert "fcntl.flock" in text, "fcntl.flock call missing in main.py"
    assert "fcntl.LOCK_EX" in text, "fcntl.LOCK_EX flag missing"
    assert "fcntl.LOCK_NB" in text, "fcntl.LOCK_NB flag missing (fails fast on conflict)"


def test_main_py_lock_path_is_var_run_moex_lock():
    """Lock file must be /var/run/moex.lock (server-side)."""
    text = MAIN_PY.read_text(encoding="utf-8")
    assert "/var/run/moex.lock" in text, "Lock path /var/run/moex.lock missing"


def test_main_py_exits_on_lock_conflict():
    """On lock conflict, sys.exit(1) must be called."""
    text = MAIN_PY.read_text(encoding="utf-8")
    # Find the except OSError block and check sys.exit is called.
    # Looking at _acquire_singleton_lock in main.py.
    match = re.search(
        r"def _acquire_singleton_lock.*?sys\.exit\(1\)",
        text,
        re.DOTALL,
    )
    assert match is not None, "sys.exit(1) missing inside _acquire_singleton_lock"


def test_main_py_calls_lock_before_asyncio_run():
    """The lock must be acquired before asyncio.run(main())."""
    text = MAIN_PY.read_text(encoding="utf-8")
    lock_idx = text.find("_acquire_singleton_lock")
    run_idx = text.find("asyncio.run(main())")
    assert lock_idx > 0, "Lock acquisition not found"
    assert run_idx > lock_idx, "asyncio.run must come AFTER lock acquisition"
