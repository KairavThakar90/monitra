# Brand assets

Drop the Monitra logo here to use it everywhere in the desktop app — the
sidebar mark, the window icon, the system-tray icon and the packaged
`.ico`/`.icns` export.

Accepted filenames (first match wins), see `core/branding.py`:

    monitra_logo.svg
    monitra_logo.png
    monitra-logo.svg
    monitra-logo.png
    logo.svg
    logo.png

`store_transform_mark.png` is different: it is the publisher's mark (the globe), not
Monitra's. It is **not used at the moment** (the system-tray icon, which Windows also
draws at the top of every notification, is the Monitra mark again). It is not one of
the names above and does not replace the Monitra mark.

Use a square, transparent-background file — SVG for the crispest result at
every size, otherwise a PNG of at least 256×256.

With no file here the app draws the vendored vector mark
(`core/branding.MONITRA_MARK_SVG`) instead. That is a real fallback, not a
placeholder: nothing renders an empty box if the folder stays empty.
