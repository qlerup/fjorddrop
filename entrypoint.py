"""Join the host Docker socket's group, then run the application without root."""
import os
from pathlib import Path
import sys

if os.getuid() == 0:
    uid, gid = int(os.getenv('PUID','1000')), int(os.getenv('PGID','1000'))
    if uid<1 or gid<1:
        raise SystemExit('PUID/PGID must be non-root')
    socket = Path('/var/run/docker.sock')
    os.setgroups([socket.stat().st_gid] if socket.exists() else [])
    os.setgid(gid)
    os.setuid(uid)
os.execvp(sys.argv[1],sys.argv[1:])
