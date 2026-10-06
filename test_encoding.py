import os
import subprocess
import sys
import tempfile

ENV = {**os.environ, "PYTHONIOENCODING": "cp1252:strict", "PYTHONUTF8": "0",
       "DISCORD_WEBHOOK_URL": "", "KIS_MOCK_APP_KEY": "", "KIS_MOCK_APP_SECRET": ""}  # hermetic: no alerts, no KIS calls


def _run(args):
    with tempfile.TemporaryFile() as f:  # redirected to a file, not a pipe
        r = subprocess.run([sys.executable, *args], stdout=f, stderr=f, env=ENV, timeout=60)
        f.seek(0)
        return r, f.read().decode("utf-8", "replace")


def test_utf8_stdio_survives_cp1252_redirect():
    r, out = _run(["-c", "import data; data.utf8_stdio(); print('KRX 로그인')"])
    assert r.returncode == 0 and "로그인" in out, out


def test_trading_dry_run_no_unicode_crash():
    _, out = _run(["trading.py", "--dry-run"])
    assert "UnicodeEncodeError" not in out, out


def test_entry_points_call_utf8_stdio():
    # a stdout crash needs live KIS + pykrx to reproduce, so just pin the wiring
    for f in ("trading.py", "run.py"):
        main_block = open(f, encoding="utf-8").read().split('if __name__ == "__main__":')[1]
        assert "data.utf8_stdio()" in main_block, f


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
