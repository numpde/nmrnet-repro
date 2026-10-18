"""Blocking private-file operations, called only by the store's owned I/O worker.

Precedent: nmrpeak-repro 1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/attempt_journal_store.py::admit/_replace_record/_require_recovery_space.
Unlike its cleanup suppression, failures here retain staging evidence and original
cleanup errors. This first owner publishes pending starts, never replaces/removes
an obligation. Directory flock excludes cooperating local owners, not other hosts.
"""

from provider_core.container_guard import require_container

require_container()

import fcntl
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from hashlib import sha256

from provider_core.attempt_record import MAX_PENDING_RECORD_BYTES, decode_record, encode_record


class StoreRefused(RuntimeError):
    """A determinate admission/ownership refusal; no new storage mutation ran."""


class StoreFailed(RuntimeError):
    """Safe storage diagnosis with protected cause and cleanup evidence.

    record_visible describes installation during this operation, not durability.
    A visible record after failure must not be dispatched until reconciled.
    """

    def __init__(self, message, *, cause=None, record_visible=None, cleanup_failures=()):
        super().__init__(message)
        self.cause = cause
        self.record_visible = record_visible
        self.cleanup_failures = tuple(cleanup_failures)


@dataclass(frozen=True, slots=True)
class StoreLimits:
    """Logical slots plus headroom for full future records and one staged write.

    recovery_record_bytes includes the largest future outcome and its evidence,
    not just today's start. Composition owns this size contract. A free-space
    check does not reserve physical blocks against unrelated filesystem writers.
    """

    maximum_records: int
    recovery_record_bytes: int
    filesystem_reserve_bytes: int

    def __post_init__(self):
        if type(self.maximum_records) is not int or self.maximum_records < 1:
            raise ValueError("Cannot configure Attempt store: maximum_records must be a positive integer")
        if type(self.recovery_record_bytes) is not int or self.recovery_record_bytes < MAX_PENDING_RECORD_BYTES:
            raise ValueError("Cannot configure Attempt store: recovery_record_bytes must cover at least the pending-record limit")
        if type(self.filesystem_reserve_bytes) is not int or self.filesystem_reserve_bytes < 0:
            raise ValueError("Cannot configure Attempt store: filesystem reserve must be a nonnegative byte count")


def record_name(record):
    # Body admission already owns Job/key grammar. The digest is a filename, not
    # proof of body integrity or permission to recover across scopes.
    key = json.loads(record.body)["provider_attempt_key"]
    return sha256(key.encode("ascii")).hexdigest() + ".json"


class AttemptFiles:
    def __init__(self, root, scope, limits):
        self.root = root
        self.scope = scope
        self.limits = limits
        self.fd = None
        self.poisoned = False
        self.close_uncertain = False

    def _usable(self):
        if self.fd is None or self.poisoned:
            raise StoreRefused(
                "Attempt storage is closed or unresolved; preserve retained files and restore storage before reuse"
            )

    def open(self):
        if self.fd is not None or self.poisoned:
            raise StoreRefused("Attempt storage is already open or unresolved")
        try:
            self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            metadata = os.fstat(self.fd)
            if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                raise StoreRefused("Attempt directory must be private, owner-controlled and mode 0700")
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StoreRefused("Another local owner holds this Attempt directory") from None
            self.records()
            # Confirm the current inventory's directory entries before authorizing
            # later use, including reopening after a lost directory-fsync result.
            os.fsync(self.fd)
        except BaseException as cause:
            cleanup = []
            try:
                self.close()
            except (OSError, StoreFailed) as error:
                cleanup.append(error)
            if isinstance(cause, StoreRefused) and not cleanup:
                raise
            self.poisoned = True
            raise StoreFailed(
                "Cannot open Attempt storage; preserve its files and inspect the protected cause",
                cause=cause,
                cleanup_failures=cleanup,
            ) from None

    def close(self):
        if self.fd is not None:
            descriptor, self.fd = self.fd, None
            self._close_descriptor(descriptor)
        if self.close_uncertain:
            raise StoreFailed("Attempt storage descriptor retirement is unresolved; retire the enclosing process")

    def _close_descriptor(self, descriptor):
        # A failed close cannot safely be retried: its number may already have
        # been reused. Keep uncertainty even after other descriptors close.
        try:
            os.close(descriptor)
        except OSError:
            self.close_uncertain = True
            self.poisoned = True
            raise

    def _read(self, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=self.fd)
        primary = None
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
            ):
                raise ValueError("Retained record must be a private singly-linked regular file")
            if metadata.st_size > MAX_PENDING_RECORD_BYTES:
                raise ValueError("Retained record exceeds the pending-record byte limit")
            chunks, total = [], 0
            while total <= MAX_PENDING_RECORD_BYTES:
                chunk = os.read(descriptor, min(65536, MAX_PENDING_RECORD_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            raw = b"".join(chunks)
            record = decode_record(raw, scope=self.scope)
            if record_name(record) != name:
                raise ValueError("Retained filename differs from the provider attempt key")
            os.fsync(descriptor)
            return record, len(raw)
        except BaseException as cause:
            primary = cause
            raise
        finally:
            try:
                self._close_descriptor(descriptor)
            except OSError as cleanup:
                raise StoreFailed(
                    "Cannot close retained-record descriptor; storage remains unresolved",
                    cause=primary,
                    cleanup_failures=(cleanup,),
                ) from None

    def _inventory(self):
        self._usable()
        found = []
        try:
            with os.scandir(self.fd) as entries:
                for entry in entries:
                    if re.fullmatch(r"[0-9a-f]{64}\.json", entry.name) is None:
                        raise ValueError("Unexpected or unfinished staging entry requires explicit recovery")
                    found.append(entry.name)
                    if len(found) > self.limits.maximum_records:
                        raise ValueError("Retained inventory exceeds configured record slots")
            return [(name, *self._read(name)) for name in sorted(found)]
        except (OSError, ValueError, StoreFailed) as cause:
            self.poisoned = True
            raise StoreFailed(
                "Cannot read Attempt inventory; retained data is unsafe or unavailable; preserve it for recovery", cause=cause
            ) from None

    def records(self):
        return tuple(record for _name, record, _size in self._inventory())

    def admit(self, record):
        self._usable()
        raw = encode_record(record)
        if record.scope != self.scope:
            raise StoreRefused("Cannot retain pending start under a different recovery scope")
        target = record_name(record)
        inventory = self._inventory()
        for name, existing, _size in inventory:
            if name == target:
                if existing != record:
                    raise StoreRefused(
                        "Provider attempt key already has different retained facts; preserve the original obligation"
                    )
                return existing
        if len(inventory) >= self.limits.maximum_records:
            raise StoreRefused("Cannot retain pending start: all durable record slots are occupied")
        try:
            space = os.fstatvfs(self.fd)
            # Existing on-disk bytes already consume free space. Reserve arithmetic
            # covers their future growth, the new full record and one full staging
            # record; unrelated writers can still invalidate this observation.
            required = (
                (len(inventory) + 2) * self.limits.recovery_record_bytes
                - sum(size for _name, _record, size in inventory)
                + self.limits.filesystem_reserve_bytes
            )
            if space.f_bavail * space.f_frsize < required:
                raise StoreRefused(
                    "Cannot retain pending start: insufficient filesystem headroom for full obligations and staged replacement"
                )
        except OSError as cause:
            self.poisoned = True
            raise StoreFailed(
                "Cannot check Attempt recovery capacity; no record was installed", cause=cause, record_visible=False
            ) from None
        self._publish(target, raw)
        return record

    def _publish(self, target, raw):
        staging = ".pending-" + secrets.token_hex(16)
        descriptor = None
        visible = False
        try:
            descriptor = os.open(
                staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.fd
            )
            os.fchmod(descriptor, 0o600)
            offset = 0
            while offset < len(raw):
                count = os.write(descriptor, raw[offset:])
                if count <= 0:
                    raise OSError("Staged record write made no progress")
                offset += count
            os.fsync(descriptor)
            closing, descriptor = descriptor, None
            self._close_descriptor(closing)
            os.link(staging, target, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
            visible = True
            os.unlink(staging, dir_fd=self.fd)
            os.fsync(self.fd)
        except BaseException as cause:
            self.poisoned = True
            cleanup = []
            if descriptor is not None:
                closing, descriptor = descriptor, None
                try:
                    self._close_descriptor(closing)
                except OSError as error:
                    cleanup.append(error)
            # Preserve partial staging and any installed target. No implicit retry
            # or deletion may erase the evidence needed to reconcile this write.
            raise StoreFailed(
                "Cannot confirm durable pending-start retention; preserve storage and reconcile before any API dispatch",
                cause=cause,
                record_visible=visible,
                cleanup_failures=cleanup,
            ) from None
