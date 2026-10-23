"""Ordered DTLS/SCTP file protocol. Bounded windows and SHA-256 for every piece."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import time
import uuid

from storage import confined

CHUNK = 16 * 1024
WINDOW = 16


def pack(index, data):
    return struct.pack('!I', index) + hashlib.sha256(data).digest() + data


def unpack(frame, expected):
    if not 36 <= len(frame) <= CHUNK + 36:
        raise ValueError('Ugyldig bidstørrelse.')
    index = struct.unpack('!I', frame[:4])[0]
    payload = frame[36:]
    if index != expected or hashlib.sha256(payload).digest() != frame[4:36]:
        raise ValueError('Integritetskontrol fejlede. Start overførslen igen.')
    return payload


class Transfer:
    def __init__(self, channel, root, destination, authorize, max_bytes):
        self.channel, self.root, self.destination = channel, root, destination
        self.authorize, self.max_bytes = authorize, max_bytes
        self.queue = asyncio.Queue(maxsize=64)
        self.file = None
        self.temp = None
        self.mode = None
        self.size = self.count = self.index = 0
        self.last = time.monotonic()
        self.task = asyncio.create_task(self.run())
        channel.on('message', self.enqueue)
        channel.on('close', self.close)

    def enqueue(self, message):
        if len(message) > CHUNK + 1024 or self.queue.full():
            self.channel.close()
            return
        self.queue.put_nowait(message)

    def send(self, value):
        if self.channel.readyState == 'open':
            self.channel.send(json.dumps(value))

    def close(self):
        self.task.cancel()
        self.reset()

    def reset(self):
        if self.file:
            self.file.close()
            self.file = None
        if self.temp:
            Path(self.temp).unlink(missing_ok=True)
            self.temp = None
            try:
                self.receipt.rmdir()
            except OSError:
                pass
        self.mode = None

    async def run(self):
        try:
            while True:
                message = await asyncio.wait_for(self.queue.get(), 120)
                self.last = time.monotonic()
                access = self.authorize()  # expiry/revocation applies to existing connections
                if isinstance(message, bytes):
                    if self.mode != 'upload':
                        raise ValueError('Start først en upload.')
                    payload = unpack(message, self.index)
                    if not payload or self.count + len(payload) > self.size:
                        raise ValueError('Forkert filstørrelse.')
                    await asyncio.to_thread(self.file.write, payload)
                    self.count += len(payload)
                    self.index += 1
                    self.send({'op': 'ack', 'bytes': self.count})
                    continue
                data = json.loads(message)
                op = data.get('op')
                if op == 'cancel':
                    self.reset()
                    self.send({'op': 'cancelled'})
                elif op == 'get':
                    self.reset()
                    path = confined(self.root, data['path'])
                    if not path.is_file():
                        raise ValueError('Vælg en fil.')
                    # The share mount is read-only and must not be writable by untrusted users.
                    self.file = path.open('rb')
                    self.size = os.fstat(self.file.fileno()).st_size
                    self.modified = os.fstat(self.file.fileno()).st_mtime_ns
                    self.count = self.index = 0
                    self.mode = 'download'
                    self.send({'op': 'meta', 'name': path.name, 'size': self.size, 'chunk': CHUNK})
                elif op == 'pull' and self.mode == 'download':
                    for _ in range(WINDOW):
                        self.authorize()
                        while self.channel.bufferedAmount > 1024 * 1024:
                            await asyncio.sleep(.01)
                        payload = await asyncio.to_thread(self.file.read, CHUNK)
                        if not payload:
                            current = os.fstat(self.file.fileno())
                            if self.count != self.size or current.st_size != self.size or current.st_mtime_ns != self.modified:
                                raise ValueError('Kildefilen ændrede sig under overførslen.')
                            self.send({'op': 'done', 'bytes': self.count})
                            self.reset()
                            break
                        if self.count + len(payload) > self.size:
                            raise ValueError('Kildefilen ændrede sig under overførslen.')
                        self.channel.send(pack(self.index, payload))
                        self.index += 1
                        self.count += len(payload)
                elif op == 'put':
                    if not access['upload']:
                        raise PermissionError('Dette link tillader kun download.')
                    self.reset()
                    name = str(data['name'])
                    if not name or len(name) > 180 or any(c in name for c in '/\\:\x00') or name.startswith('.') or any(ord(c)<32 for c in name):
                        raise ValueError('Ugyldigt filnavn.')
                    size = data['size']
                    if type(size) is not int or not 0 <= size <= self.max_bytes:
                        raise ValueError('Filen overstiger uploadgrænsen.')
                    self.destination.mkdir(parents=True, exist_ok=True)
                    if shutil.disk_usage(self.destination).free < size + 64*1024*1024:
                        raise ValueError('Der er ikke plads i destinationsmappen.')
                    self.size, self.count, self.index = size, 0, 0
                    # A unique receipt directory prevents overwrite and filename races.
                    self.receipt = self.destination / uuid.uuid4().hex[:16]
                    self.receipt.mkdir()
                    self.final = self.receipt / name
                    fd, self.temp = tempfile.mkstemp(prefix='.partial-', dir=self.receipt)
                    self.file = os.fdopen(fd, 'wb')
                    self.mode = 'upload'
                    self.send({'op': 'ready'})
                elif op == 'finish' and self.mode == 'upload':
                    if self.count != self.size:
                        raise ValueError('Filen er ikke komplet.')
                    await asyncio.to_thread(self.file.flush)
                    await asyncio.to_thread(os.fsync, self.file.fileno())
                    self.file.close()
                    self.file = None
                    os.replace(self.temp, self.final)
                    self.temp = None
                    self.mode = None
                    self.send({'op': 'saved', 'name': self.final.name,
                               'path': self.final.relative_to(self.destination).as_posix(), 'bytes': self.count})
                else:
                    raise ValueError('Ugyldig overførselskommando.')
        except asyncio.CancelledError:
            pass
        except (ValueError, KeyError, TypeError, PermissionError, OSError, asyncio.TimeoutError) as exc:
            self.send({'op': 'error', 'error': str(exc) if not isinstance(exc, OSError) else 'Filen kunne ikke læses eller gemmes.'})
        finally:
            self.reset()
