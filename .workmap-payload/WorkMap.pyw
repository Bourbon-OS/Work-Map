"""Open the selected Map; without arguments, open this kit's default Map."""
import sys
from workmap import main

if __name__ == '__main__':
    # Preserve the former no-argument launcher, including callers using runpy.
    args = sys.argv[1:] if any(a == '--map-dir' or a.startswith('--map-dir=') for a in sys.argv[1:]) else []
    result = main(['gui', *args])
    if result:
        from tkinter import messagebox
        messagebox.showerror('Work Map', '起動できませんでした。READMEの初期設定と、PowerShellからの python workmap.py gui を確認してください。')
    raise SystemExit(result)
