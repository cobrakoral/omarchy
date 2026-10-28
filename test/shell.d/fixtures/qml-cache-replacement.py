#!/usr/bin/python3
"""Reproduce Qt's compile/read-checksum race using only fixture file replacement."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time

repo, out = map(Path, sys.argv[1:3])
launcher = Path(sys.argv[3]) if len(sys.argv) > 3 else repo / "bin/omarchy-launch-shell"
out.mkdir(exist_ok=True)
config = out / "root/shell"
for name in ("plugins", "Commons"):
    (config / name).mkdir(parents=True, exist_ok=True)
for name in ("home", "cache", "config", "data", "state", "run", "bin"):
    (out / name).mkdir()
(out / "run").chmod(0o700)
(out / "bin/systemd-cat").write_text('#!/bin/bash\nwhile (( $# )); do [[ $1 == "--" ]] && { shift; break; }; shift; done\nexec "$@"\n')
(out / "bin/systemd-cat").chmod(0o755)
env = dict(os.environ, HOME=str(out / "home"), OMARCHY_PATH=str(config.parent),
           XDG_CONFIG_HOME=str(out / "config"), XDG_CACHE_HOME=str(out / "cache"),
           XDG_DATA_HOME=str(out / "data"), XDG_STATE_HOME=str(out / "state"),
           XDG_RUNTIME_DIR=str(out / "run"), QT_QPA_PLATFORM="offscreen",
           QSG_RHI_BACKEND="software", QS_DISABLE_FILE_WATCHER="1", QS_NO_RELOAD_POPUP="1",
           QT_LOGGING_RULES="qt.qml.diskcache.debug=true", PATH=str(out / "bin")+":"+os.environ["PATH"])
for name in ("WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE", "QML_DISK_CACHE_PATH", "QML_DISABLE_DISK_CACHE", "LD_PRELOAD"):
    env.pop(name, None)
settings = out / "config/retained-settings.json"
settings.write_text('{"keep":"settings"}\n')
(config / "shell.qml").write_text("""//@ pragma OmarchyLoadPatch
import QtQuick 6.11
import Quickshell
import Quickshell.Io
ShellRoot {
 id: root
 property var component: null
 property var instance: null
 IpcHandler {
  target: "control"
  function load(): string {
   root.component=Qt.createComponent("file://"+Quickshell.shellDir+"/plugins/Plugin.qml",Component.PreferSynchronous)
   if(root.component.status===Component.Ready) root.instance=root.component.createObject(root)
   return root.instance ? root.instance.proof : root.component.errorString()
  }
  function ping(): string {return "ok"}
 }
}
""")
target = config / "plugins/Plugin.qml"
replacement = config / "plugins/replacement.txt"
a = 'import QtQuick 6.11\nimport qs.Commons\nItem { ShellIpc {id: old} property string proof: old.version }\n'
b = 'import QtQuick 6.11\nimport Quickshell.Io\nItem {property string proof: "B"; IpcHandler {target: "representative"; function proof(): string {return "B"}}}\n'
def version_a():
    (config / "Commons/qmldir").write_text("module qs.Commons\nShellIpc 1.0 ShellIpc.qml\n")
    (config / "Commons/ShellIpc.qml").write_text('import QtQuick 6.11\nimport Quickshell.Io\nIpcHandler {property string version: "A"; target: "representative"; function proof(): string {return "A"}}\n')
    target.write_text(a)
    replacement.write_text(b)

def complete_b():
    (config / "Commons/qmldir").write_text("module qs.Commons\nDummy 1.0 Dummy.qml\n")
    (config / "Commons/Dummy.qml").write_text("import QtQuick 6.11\nQtObject {}\n")
    (config / "Commons/ShellIpc.qml").unlink()
    assert target.read_text() == b

shim = out / "replace-on-read.so"
subprocess.run(["cc", "-shared", "-fPIC", "-O2", str(repo / "test/shell.d/fixtures/qml-cache-replace-on-read.c"), "-ldl", "-o", str(shim)], check=True)

def ipc(method, target_name="control"):
    r = subprocess.run(["quickshell", "ipc", "-p", str(config), "call", target_name, method],
                       env=env, text=True, capture_output=True, timeout=3)
    if r.returncode:
        raise RuntimeError(r.stderr or r.stdout)
    return r.stdout.strip()

def run(label, through_launcher=False, inject=False):
    child_env = dict(env)
    if inject:
        child_env.update(LD_PRELOAD=str(shim), O16126_TARGET=str(target),
                         O16126_REPLACEMENT=str(replacement), O16126_SKIP_READS="1", O16126_MARKER=str(out / "replacement.marker"))
    command = [str(launcher)] if through_launcher else ["quickshell", "-p", str(config), "--no-color"]
    with (out / (label + ".log")).open("w") as log:
        p = subprocess.Popen(command, env=child_env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if p.poll() is not None:
                    raise RuntimeError(label + " exited before IPC")
                try:
                    if ipc("ping") == "ok":
                        break
                except RuntimeError:
                    pass
                time.sleep(.02)
            else:
                raise RuntimeError(label + " IPC timeout")
            value = ipc("load")
            proof = ipc("proof", "representative")
            print(json.dumps({"case": label, "value": value, "ipc": proof}), flush=True)
            return value, proof
        finally:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=3)

version_a()
assert run("compile-old-source-after-replacement", inject=True) == ("A", "A")
assert (out / "replacement.marker").read_text() == "replaced after source read\n"
cache = list((out / "cache").rglob("*.qmlc"))
assert cache, "real Qt must write the poisoned cache"
unit = cache[0].read_bytes()
assert struct.unpack_from("<q", unit, 16)[0] == -1
assert unit[28:44] == hashlib.md5(b.encode()).digest(), "old bytecode was saved with the replacement's hash"
shutil.copytree(out / "cache", out / "poisoned-cache")
complete_b()
for n in range(2):
    failure, _ = run("baseline-restart-" + str(n))
    assert "ShellIpc is not a type" in failure, failure

assert run("launcher-recovery", through_launcher=True) == ("B", "B")
assert run("launcher-repeated-recovery", through_launcher=True) == ("B", "B")
assert cache[0].read_bytes() == unit, "recovery must not delete or rewrite existing caches"
# The same replacement while running the real launcher must not create new
# cache entries. Clearing this isolated cache is only a test control.
shutil.move(out / "cache", out / "retained-cache")
(out / "cache").mkdir()
version_a()
assert run("launcher-prevention", through_launcher=True, inject=True) == ("A", "A")
assert not list((out / "cache").rglob("*.qmlc")), "launcher must prevent poisoned cache writes"
complete_b()
assert run("launcher-after-repair", through_launcher=True) == ("B", "B")
assert run("fresh-cache-control") == ("B", "B")
assert settings.read_text() == '{"keep":"settings"}\n'
print("ok - real launcher prevents and bypasses persistent QML cache poisoning", flush=True)
