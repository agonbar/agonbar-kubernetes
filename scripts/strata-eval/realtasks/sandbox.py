"""bubblewrap sandbox shared by the agents and the verifiers.

Base: ~/dotfiles/scripts/pi-sandboxed.sh. The whole filesystem is visible read-only, but /tmp,
the user's $HOME and /run are empty tmpfs: no ~/.ssh, ~/.kube, ~/.config/gh, agenix secrets,
ssh/gpg agent or docker socket. KUBECONFIG points nowhere, no GH_TOKEN. The nix-daemon socket
is hidden too, except for tasks marked nix=True (nix eval of the dotfiles flake needs the store).
Only the workspace and the run's own HOME are writable. The network is shared (docs, crates.io,
the model endpoints). The toolchains (cargo, rustc, cc, kubeconform, shellcheck) come from Nix
store paths on PATH, built by toolchain() outside the sandbox.
"""
import os, shutil, subprocess

REAL_HOME = os.path.expanduser("~")
TC_DIR = os.environ.get("REALTASKS_TC", "/tmp/realtasks/tc")
TOOLS = {  # out-link name -> installable
    "cargo": "nixpkgs#cargo", "rustc": "nixpkgs#rustc", "gcc": "nixpkgs#gcc", "clippy": "nixpkgs#clippy",
    "rustfmt": "nixpkgs#rustfmt", "pkg-config": "nixpkgs#pkg-config", "kubeconform": "nixpkgs#kubeconform",
    "shellcheck": "nixpkgs#shellcheck", "bash-ni": "nixpkgs#bashNonInteractive",
}
BWRAP = shutil.which("bwrap")  # absolute: the sandbox env PATH may hold stubs named bwrap
PYENV_EXPR = '(import (builtins.getFlake "nixpkgs") {}).python3.withPackages (p: [p.pyyaml])'


def toolchain():
    """Build (and GC-root under TC_DIR) every tool; return the bin dirs for PATH."""
    os.makedirs(TC_DIR, exist_ok=True)
    for name, inst in TOOLS.items():
        if not os.path.exists(os.path.join(TC_DIR, name)) and not os.path.exists(os.path.join(TC_DIR, name + "-bin")):
            subprocess.run(["nix", "build", "--out-link", os.path.join(TC_DIR, name), inst], check=True)
    if not os.path.exists(os.path.join(TC_DIR, "pyenv")):
        subprocess.run(["nix", "build", "--impure", "--out-link", os.path.join(TC_DIR, "pyenv"), "--expr", PYENV_EXPR], check=True)
    return [os.path.realpath(os.path.join(TC_DIR, n + ("-bin" if n == "shellcheck" else ""))) + "/bin" for n in TOOLS if n != "bash-ni"]


def tool(name):
    return os.path.realpath(os.path.join(TC_DIR, name))


def env(home, extra_path=(), **kw):
    path = list(extra_path) + toolchain() + ["/etc/profiles/per-user/%s/bin" % os.environ["USER"], "/run/current-system/sw/bin"]
    e = {"PATH": ":".join(path), "HOME": home, "USER": os.environ["USER"], "LANG": "C.UTF-8", "TERM": "dumb",
         "XDG_CONFIG_HOME": home + "/.config", "XDG_DATA_HOME": home + "/.local/share",
         "XDG_CACHE_HOME": home + "/.cache", "XDG_STATE_HOME": home + "/.local/state",
         "GIT_AUTHOR_NAME": "agent", "GIT_AUTHOR_EMAIL": "agent@sandbox.invalid",
         "GIT_COMMITTER_NAME": "agent", "GIT_COMMITTER_EMAIL": "agent@sandbox.invalid",
         "GIT_TERMINAL_PROMPT": "0", "KUBECONFIG": "/nonexistent",
         "CARGO_HOME": home + "/.cargo", "CARGO_BUILD_JOBS": "6", "CARGO_PROFILE_DEV_DEBUG": "0",
         "CARGO_PROFILE_TEST_DEBUG": "0", "CARGO_NET_RETRY": "10", "GOTOOLCHAIN": "local", "GOPATH": home + "/go",
         "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt", "NIX_SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt"}
    e.update(kw)
    return e


def wrap(cmd, rw, chdir, nix=False, ro=()):
    """bwrap argv: rw = writable paths (workspace, HOME, run dir), ro = extra read-only binds."""
    a = [BWRAP, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
         "--tmpfs", "/tmp", "--tmpfs", REAL_HOME, "--tmpfs", "/run",
         "--ro-bind", "/run/current-system", "/run/current-system",
         "--ro-bind-try", "/run/systemd/resolve", "/run/systemd/resolve"]
    if not nix:
        a += ["--tmpfs", "/nix/var/nix/daemon-socket"]
    for p in ro:
        a += ["--ro-bind", p, p]
    for p in rw:
        a += ["--bind", p, p]
    return a + ["--chdir", chdir, "--unshare-pid", "--die-with-parent", "--"] + list(cmd)


def run(cmd, ws, home, nix=False, timeout=600, ro=(), rw=(), **envkw):
    """Run cmd sandboxed in ws; returns CompletedProcess (text). Used by the verifiers."""
    os.makedirs(home, exist_ok=True)
    global LAST
    LAST = (wrap(cmd, [ws, home] + list(rw), ws, nix, ro), env(home, **envkw))
    try:
        # stdin must not be inherited: NixOS bash treats a socket on stdin as an ssh session and
        # sources /etc/bashrc, which resets PATH (the Bash tool of an agent harness hands one down).
        return subprocess.run(LAST[0], env=LAST[1], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(cmd, 124, (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
                                           "TIMEOUT after %ss" % timeout)
