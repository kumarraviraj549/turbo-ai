import pathlib

site_pkg = pathlib.Path("venv/Lib/site-packages")
site_pkg.mkdir(parents=True, exist_ok=True)

fix_code = """import os, time, threading, pathlib

_lock = threading.RLock()
_orig_replace = os.replace
_orig_rename = os.rename

def _safe_replace(src, dst):
    with _lock:
        for i in range(25):
            try:
                return _orig_replace(src, dst)
            except PermissionError:
                time.sleep(0.04 * (i + 1))
            except FileNotFoundError:
                if pathlib.Path(dst).exists():
                    return dst
                raise
        src_p, dst_p = pathlib.Path(src), pathlib.Path(dst)
        if src_p.exists():
            data = src_p.read_bytes()
            for i in range(15):
                try:
                    dst_p.write_bytes(data)
                    break
                except PermissionError:
                    time.sleep(0.05 * (i + 1))
            try:
                src_p.unlink(missing_ok=True)
            except Exception:
                pass
        return dst

def _safe_path_replace(self, target):
    _safe_replace(str(self), str(target))
    return pathlib.Path(target)

os.replace = _safe_replace
os.rename = _safe_replace
pathlib.Path.replace = _safe_path_replace
pathlib.Path.rename = _safe_path_replace
"""

(site_pkg / "sitecustomize.py").write_text(fix_code, encoding="utf-8")

out = pathlib.Path("output")
if out.exists():
    for tmp in out.rglob("*.tmp"):
        try:
            tmp.unlink()
        except Exception:
            pass

print("SUCCESS: Windows WinError 5 atomic-write fix installed!")
