"""Child process of ``test_launcher_wine.py``: the launcher's bridge path, then exec Wine.

Run as ``python3 launcher_child.py CONFIG.json``. It does exactly what
``fork_linux.launcher.run`` does for the bridge (``bridge.start_daemon`` on the main
thread, ``launcher.build_spec`` with ``bridge.launch_env``, ``launcher.write_session``,
``launcher._exec``), except that Wine runs the bridged ``sh.exe`` with the given script
instead of ``Fork.exe``. The process therefore *becomes* the Wine process the daemon's
``--parent-pid`` names, as Fork's does.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path


def main() -> int:
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    sys.path.insert(0, config["src"])
    from fork_linux import bootstrap, bridge, launcher
    from fork_linux import manifest as manifest_mod
    from fork_linux.config import Config
    from fork_linux.paths import Paths
    from fork_linux.procrun import Runner
    from fork_linux.state import State
    from fork_linux.ui import NullUI
    from fork_linux.winecmd import WineInfo

    env = dict(os.environ)
    paths = Paths.from_env(env)
    ctx = bootstrap.Ctx(
        paths=paths,
        config=Config.load(paths, env),
        manifest=manifest_mod.load(),
        runner=Runner(),
        env=env,
        ui=NullUI(),
        state=State.load(paths.state_file),
        user=config["user"],
    )
    wine = Path(config["wine"])
    ctx.set_wine(
        WineInfo(
            provider="system",
            build_id=None,
            root=wine.parent.parent,
            wine=wine,
            wineserver=Path(config["wineserver"]),
            version="",
            staging=False,
            wow64=False,
        )
    )
    daemon = bridge.start_daemon(ctx)
    if daemon is None:
        print("bridge daemon not started", file=sys.stderr)
        return 3
    spec = launcher.build_spec(ctx, [], bridge_env=bridge.launch_env(ctx, daemon))
    spec = dataclasses.replace(
        spec,
        argv=[str(wine), "C:\\fork-linux\\gitInstance\\bin\\sh.exe", "-c", config["script"]],
        cwd=Path(config["cwd"]),
        log_file=None,
    )
    launcher.write_session(paths, ctx.wine(), os.getpid(), bridge.session_record(daemon))
    return launcher._exec(spec, truncate=False, execvpe=os.execvpe)


if __name__ == "__main__":
    raise SystemExit(main())
