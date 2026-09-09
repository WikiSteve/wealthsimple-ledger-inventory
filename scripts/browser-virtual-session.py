#!/usr/bin/env python3
"""Supervise a private X desktop and localhost-only noVNC viewer as one unit."""
import argparse
import os
from pathlib import Path
import secrets
import signal
import socket
import string
import subprocess
import time


def wait_for(probe, children, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for p in children:
            if p.poll() is not None:
                raise RuntimeError('Virtual desktop component exited: ' +
                                   Path(p.args[0]).name + ' rc=' + str(p.returncode))
        if probe():
            return
        time.sleep(.2)
    raise RuntimeError('Virtual desktop readiness timed out')


def listening(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.2):
            return True
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--browser', required=True)
    parser.add_argument('--port', type=int, default=9223)
    parser.add_argument('--viewer-port', type=int, default=6083)
    parser.add_argument('--vnc-port', type=int, default=5903)
    parser.add_argument('--display', type=int, default=93)
    args = parser.parse_args()
    os.umask(0o077)
    args.state.mkdir(parents=True, exist_ok=True)
    args.state.chmod(0o700)
    display = ':' + str(args.display)
    if Path(f'/tmp/.X11-unix/X{args.display}').exists() or Path(f'/tmp/.X{args.display}-lock').exists():
        raise RuntimeError('Display is occupied; will not replace it')
    if any(listening(p) for p in (args.port, args.viewer_port, args.vnc_port)):
        raise RuntimeError('Port is occupied; will not reuse an unrelated service')
    auth = args.state / 'Xauthority'
    auth.touch(mode=0o600, exist_ok=True)
    # Supply the X cookie over stdin, never process argv or logs.
    subprocess.run(['xauth', '-f', str(auth)], input=(
        'add ' + display + ' . ' + secrets.token_hex(16) + '\n').encode(),
        check=True, stdout=subprocess.DEVNULL)
    password = args.state / 'viewer-password'
    password.write_text(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8)) + '\n')
    password.chmod(0o600)
    env = dict(os.environ, DISPLAY=display, XAUTHORITY=str(auth))
    env.pop('WAYLAND_DISPLAY', None)
    children = []
    def start(argv):
        p = subprocess.Popen(argv, env=env)
        children.append(p)
        return p
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        start(['Xvfb', display, '-screen', '0', '1440x1000x24', '-noreset', '-nolisten', 'tcp', '-auth', str(auth)])
        wait_for(lambda: Path(f'/tmp/.X11-unix/X{args.display}').exists(), children)
        # A private WM bus avoids colliding with the operator's desktop WM.
        # Chromium keeps the user bus so its existing keyring remains available.
        start(['dbus-run-session', '--', 'metacity', '--sm-disable', '--compositor=none'])
        start(['x11vnc', '-norc', '-display', display, '-auth', str(auth), '-listen', '127.0.0.1',
               '-no6', '-rfbport', str(args.vnc_port), '-passwdfile', str(password),
               '-forever', '-shared', '-noxdamage'])
        wait_for(lambda: listening(args.vnc_port), children)
        start(['websockify', '--web=/usr/share/novnc',
               '127.0.0.1:' + str(args.viewer_port), '127.0.0.1:' + str(args.vnc_port)])
        start([args.browser, '--ozone-platform=x11', '--user-data-dir=' + args.profile,
               '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=' + str(args.port),
               '--no-first-run', '--no-default-browser-check', '--disable-session-crashed-bubble',
               '--disable-background-timer-throttling', '--disable-backgrounding-occluded-windows',
               '--disable-renderer-backgrounding', '--window-size=1400,960', '--new-window',
               'https://my.wealthsimple.com/app/docs'])
        wait_for(lambda: listening(args.port) and listening(args.viewer_port), children, 30)
        print('Virtual browser ready; viewer http://127.0.0.1:' + str(args.viewer_port) + '/vnc.html', flush=True)
        while all(p.poll() is None for p in children):
            time.sleep(1)
        raise RuntimeError('Virtual desktop component exited')
    except KeyboardInterrupt:
        pass
    finally:
        for p in reversed(children):
            if p.poll() is None:
                p.terminate()
        for p in reversed(children):
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()


if __name__ == '__main__':
    main()
