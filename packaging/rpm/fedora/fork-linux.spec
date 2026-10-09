# Fedora RPM spec for Fork for Linux (unofficial).
# The version comes from the repo-root VERSION file at build time (AGENTS.md §4.7.2):
#   rpmbuild -ba -D "fl_version $(python3 scripts/read-version.py)" fork-linux.spec
# No scriptlets (AGENTS.md §4.9): the package ships code only; all Wine state is per user.

%global fl_version %{?fl_version}%{!?fl_version:0.0.0}
%global app_id io.github.ventura8.ForkLinux

Name:           fork-linux
Version:        %{fl_version}
Release:        1%{?dist}
Summary:        Unofficial Wine wrapper for the Fork git client
License:        MIT
URL:            https://github.com/ventura8/Fork-Linux
Source0:        %{url}/archive/v%{version}/fork-linux-%{version}.tar.gz
ExclusiveArch:  x86_64

BuildRequires:  meson >= 0.61
BuildRequires:  ninja-build
BuildRequires:  gcc
BuildRequires:  python3 >= 3.10
BuildRequires:  mingw64-gcc
BuildRequires:  mingw64-winpthreads-static
BuildRequires:  desktop-file-utils
BuildRequires:  appstream

# Wine's host libraries are required by soname: the managed Wine runtime (downloaded
# per user at first run) loads them; the wrapper itself links none of them.
Requires:       python3 >= 3.10
Requires:       cabextract
Requires:       unzip
Requires:       libfreetype.so.6()(64bit)
Requires:       libfontconfig.so.1()(64bit)
Requires:       libgnutls.so.30()(64bit)
Requires:       libX11.so.6()(64bit)
Requires:       libXext.so.6()(64bit)
Requires:       libXrender.so.1()(64bit)
Requires:       libXi.so.6()(64bit)
Requires:       libXrandr.so.2()(64bit)
Requires:       libXcursor.so.1()(64bit)
Requires:       libXcomposite.so.1()(64bit)
Requires:       libXinerama.so.1()(64bit)
Requires:       libXfixes.so.3()(64bit)
Requires:       libGL.so.1()(64bit)
Recommends:     (zenity or kdialog)
Recommends:     git
Recommends:     xdg-utils
Recommends:     7zip
Recommends:     libvulkan.so.1()(64bit)
Recommends:     libdbus-1.so.3()(64bit)
Recommends:     libasound.so.2()(64bit)
Recommends:     libpulse.so.0()(64bit)
Recommends:     libxkbcommon.so.0()(64bit)
Recommends:     libwayland-client.so.0()(64bit)
Recommends:     libEGL.so.1()(64bit)
Suggests:       xdotool

%description
Fork for Linux (unofficial) runs the official, unmodified Fork for Windows git
client under Wine, with a menu entry, a fork command for opening repositories,
update snapshots with rollback and a health check.

Fork is proprietary, paid software made by Dan Pristupov and Tanya Pristupova
(https://git-fork.com). The evaluation is free; please buy a license at
https://git-fork.com/buy if you keep using it.

This package is not affiliated with, endorsed by or supported by the Fork
developers. It contains only the wrapper: at first run it downloads a pinned
Wine runtime, Microsoft .NET Framework (under Microsoft's terms) and the
official Fork installer from Fork's own download server into the user's home
directory. It never redistributes, patches or reverse-engineers Fork, never
uses the wine package of the system and never touches ~/.wine. Report wrapper
problems at https://github.com/ventura8/Fork-Linux/issues.

%prep
%autosetup -n fork-linux-%{version}

%build
# The bridge's C unit tests run in CI (lint / compat / bridge stages); %%check runs the
# data checks (generated files current, desktop-file-validate, appstreamcli).
%meson -Dbridge=enabled -Dflavor=rpm -Dpython=/usr/bin/python3 -Dfork_alias=true -Dtests=false
%meson_build

%install
%meson_install

%check
%meson_test
desktop-file-validate %{buildroot}%{_datadir}/applications/%{app_id}.desktop
appstreamcli validate --no-net %{buildroot}%{_datadir}/metainfo/%{app_id}.metainfo.xml

%files
%license LICENSE
%doc README.md
%{_bindir}/fork-linux
%{_bindir}/fork
%{_datadir}/fork-linux/
%{_prefix}/lib/fork-linux/
%{_datadir}/applications/%{app_id}.desktop
%{_datadir}/metainfo/%{app_id}.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/%{app_id}.svg
%{_datadir}/icons/hicolor/symbolic/apps/%{app_id}-symbolic.svg
%{_mandir}/man1/fork-linux.1*
%{_mandir}/man1/fork.1*
%{_datadir}/bash-completion/completions/fork-linux
%{_datadir}/bash-completion/completions/fork
%{_datadir}/zsh/site-functions/_fork-linux
%{_datadir}/fish/vendor_completions.d/fork-linux.fish
%{_datadir}/fish/vendor_completions.d/fork.fish

%changelog
* Fri Oct 09 2026 Sergiu Alexandrescu <alexandrescu.sergiu@gmail.com> - 0.1.0-1
- First public release
