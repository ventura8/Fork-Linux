# Credits & thanks

Fork for Linux (unofficial) only makes the official Fork for Windows start, integrate and update safely on Linux. **Everything that makes Fork great is the work of its developers.**

## Fork and its developers

[Fork](https://git-fork.com) is made by **Dan Pristupov** and **Tanya Pristupova**.

| | |
|---|---|
| Website | https://git-fork.com |
| Buy a license | https://git-fork.com/buy |
| Windows release notes | https://git-fork.com/releasenoteswin |
| License / EULA | https://git-fork.com/license |
| Twitter / X | [@git_fork](https://twitter.com/git_fork) |
| Issue trackers | [fork-dev/TrackerWin](https://github.com/fork-dev/TrackerWin) (Windows) · [fork-dev/Tracker](https://github.com/fork-dev/Tracker) (Mac and general) |
| Email | support@fork.dev — **licensing questions only** |

### Please buy a license

Fork is proprietary, paid software. The evaluation is free; a license is a one-time purchase ($59.99 at the time of writing) for one user on up to three machines. **If you use Fork — on Linux too — please [buy a license](https://git-fork.com/buy).** It is what keeps Fork alive.

### Where to report problems

> [!IMPORTANT]
> Fork for Linux (unofficial) is **not affiliated with, endorsed by, or supported by** Fork's developers.
>
> - **Do not send wrapper, Wine or Linux issues to Fork support** or to Fork's trackers. Report them at https://github.com/ventura8/Fork-Linux/issues.
> - If a problem **also happens on Windows**, it is a Fork bug: report it to [fork-dev/TrackerWin](https://github.com/fork-dev/TrackerWin).

## Community prior art

This project stands on earlier community work. Thank you to everyone who explored Fork under Wine first:

- [pixiekat's Wine guide](https://github.com/pixiekat/gists/blob/main/install-wine-and-delinea.md), section 10 "Bonus: Fork (Git client) under Wine" — a Wine recipe for Fork that works without patching it.
- [jasonnicholson/fork-wine-setup](https://github.com/jasonnicholson/fork-wine-setup) — setup automation and a launcher. Note: it patches `Fork.exe`; we deliberately do not.
- NitroHxC's gists ([setup and run scripts](https://gist.github.com/NitroHxC/ff579d57b15f7ba5dcd1429eac13468f), [Docker proof of concept](https://gist.github.com/NitroHxC/19268b7b6c113da585d6b1c505e2bf71)) — the `RELEASES` feed / full-nupkg approach.
- [fork-dev/Tracker#2033 "Run Fork with Wine"](https://github.com/fork-dev/Tracker/issues/2033) — the developer-run thread on running Fork under Wine.
- [fork-dev/Tracker#153](https://github.com/fork-dev/Tracker/issues/153) — the long-standing request for a Linux version.
- [WineHQ AppDB entry for Fork](https://appdb.winehq.org/objectManager.php?sClass=version&iId=42350) — community test reports.
- [dakusan/tortoisewine](https://github.com/dakusan/tortoisewine) and [coskunergan/ubuntu_tortoise](https://github.com/coskunergan/ubuntu_tortoise) — prior art for bridging a Windows git GUI under Wine to the native Linux git.
- [andy-5/wslgit](https://github.com/andy-5/wslgit) — a Windows → WSL git forwarder, including handling of Fork's interactive rebase helper.

## Third-party components & licenses

Fork for Linux's own code is MIT-licensed (see [LICENSE](../LICENSE)). At runtime it downloads or uses these components, each under its own terms; none of them is redistributed in this repository:

| Component | Role | License / terms |
|---|---|---|
| [Wine](https://www.winehq.org) | Runs Fork for Windows on Linux | LGPL-2.1-or-later |
| [Kron4ek Wine-Builds](https://github.com/Kron4ek/Wine-Builds) | The pinned, sha256-verified managed Wine runtime (staging, WoW64), downloaded per user | Wine's LGPL-2.1-or-later (build scripts MIT) |
| [Winetricks](https://github.com/Winetricks/winetricks) | Installs .NET Framework and the core fonts into the prefix | LGPL-2.1-or-later |
| [Flathub `org.winehq.Wine` BaseApp](https://github.com/flathub/org.winehq.Wine) | Wine inside the Flatpak build | Wine's LGPL-2.1-or-later |
| [mingw-w64](https://www.mingw-w64.org) runtime | Statically linked into our Windows shims | Permissive (ZPL-2.1 / MIT / public domain, see its COPYING) |
| [musl](https://musl.libc.org) | Statically linked into our Linux bridge helper | MIT |
| [appimagetool](https://github.com/AppImage/appimagetool) | Builds the AppImage | MIT |
| Microsoft .NET Framework 4.8 | Required by Fork; installed by winetricks from Microsoft's servers | Microsoft .NET Framework redistributable license terms |
| Microsoft core fonts | Font rendering in the prefix; installed by winetricks | Microsoft core fonts EULA |

## Trademarks

"Fork" and the Fork logo are trademarks of their respective owners and are used here only to describe what this wrapper runs. This project ships no Fork binaries, logos, icons or screenshots: Fork is downloaded from its official CDN on your machine, and its icon is extracted from your own copy at runtime. Other names are trademarks of their respective owners.
