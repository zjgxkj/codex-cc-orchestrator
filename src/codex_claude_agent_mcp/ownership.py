"""Process-lifetime OS locks: no PID reuse, lease expiry, heartbeat or daemon.

Lock files are deliberately retained: unlinking a lock file can split owners
between two inodes. State storage must be on a local filesystem (like SQLite).
"""

import os
import uuid
from pathlib import Path


def _lock(file) -> None:
    file.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


class ProcessOwner:
    def __init__(self, db_path: Path):
        self.directory = db_path.resolve().with_suffix(db_path.suffix + ".owners")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.owner_id = uuid.uuid4().hex
        self.file = (self.directory / self.owner_id).open("x+b")
        self.file.write(b"1")
        self.file.flush()
        _lock(self.file)

    def is_dead(self, owner_id: str) -> bool:
        # Unknown identities are not proof of death. Do not allow path traversal.
        if len(owner_id) != 32 or any(c not in "0123456789abcdef" for c in owner_id):
            return False
        try:
            file = (self.directory / owner_id).open("r+b")
        except OSError:
            return False
        with file:
            try:
                _lock(file)
            except OSError:
                return False
            return True  # Closing releases our probe lock; owner is already dead.

    def close(self) -> None:
        self.file.close()
