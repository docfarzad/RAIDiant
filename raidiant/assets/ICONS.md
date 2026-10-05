# Application icons

All application, window, taskbar, home-screen, and header artwork comes from the
user-supplied `RAIDiant Icons.zip` (2026-10-04). The previous artwork is retired.

`app-icon.icns` is the supplied `macOS/AppIcon.icns`, copied byte-for-byte.
Its SHA-256 is
`4163d7bdb598c5cb98bd4e8a84458f91265454fed06dd4928b2e12aaeb03afe5`.

## macOS

The supplied ICNS contains all ten standard and Retina representations:

| Logical size | Scale | Pixels | ICNS tag |
| --- | --- | --- | --- |
| 16 | 1x | 16 × 16 | `ic04` (ARGB) |
| 16 | 2x | 32 × 32 | `ic11` (PNG) |
| 32 | 1x | 32 × 32 | `ic05` (ARGB) |
| 32 | 2x | 64 × 64 | `ic12` (PNG) |
| 128 | 1x | 128 × 128 | `ic07` (PNG) |
| 128 | 2x | 256 × 256 | `ic13` (PNG) |
| 256 | 1x | 256 × 256 | `ic08` (PNG) |
| 256 | 2x | 512 × 512 | `ic14` (PNG) |
| 512 | 1x | 512 × 512 | `ic09` (PNG) |
| 512 | 2x | 1024 × 1024 | `ic10` (PNG) |

The container also includes its original `info` metadata. The asset-catalog PNGs
at 16, 32, 64, 128, 256, 512, and 1024 pixels are copied unchanged to
`app-icon-*.png`. The 1024-pixel representation is supplied in this set, not
upscaled from the old artwork. Finder/Dock rendering needs native macOS verification.

## Windows and Linux

- `app-icon.ico` contains 16, 20, 24, 32, 40, 48, 64, 72, 96, 128, 160, 192,
  and 256-pixel PNG frames for list, taskbar, display scaling, and large-icon views.
- The supplied favicon folder contributes unchanged 48, 72, 96, and 192-pixel
  PNGs. The missing 20, 24, 40, and 160-pixel sizes are bicubic downsampled from
  the supplied 1024-pixel PNG, without redrawing or changing the artwork.
- `app-icon-*.png` provides window icons and Linux desktop integration.
- `RAIDiant.desktop` is the optional Linux application launcher; the build copies
  it and the 512-pixel PNG to `dist`. See the README for installation commands.

Normal builds use the committed assets and require no image conversion packages.
Maintainers can regenerate the Windows ICO and four scaling variants from the
committed PNGs with PowerShell 7 on Windows:

```powershell
pwsh -File tools/generate-icons.ps1
```

This command preserves the original ICNS and all supplied PNGs. Unused mobile/web
assets from the archive and obsolete artwork are not shipped in the executable.
