"""Blocking private-file operations, called only by the store's owned I/O worker.

Precedent: nmrpeak-repro 1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/attempt_journal_store.py::admit/_replace_record/_require_recovery_space.
Unlike its cleanup suppression, failures here retain staging evidence and original
cleanup errors. Replacement requires the exact prior record and a forward lifecycle
transition. Directory flock excludes cooperating local owners, not other hosts.
"""

from provider_core.container_guard import require_container

require_container()

import fcntl
import json
import os
import re
import secrets
import stat
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from hashlib import sha256

from provider_core.attempt_record import (
    MAX_RECORD_BYTES,
    StartPending,
    TerminalConfirmed,
    decode_record,
    encode_record,
    pending_start,
    require_transition,
)


class StoreRefused(RuntimeError):
    """A determinate admission/ownership refusal; no new storage mutation ran."""


class StoreFailed(RuntimeError):
    """Safe storage diagnosis with protected cause and cleanup evidence.

    record_visible reports whether the operation observed its target entry, or
    None if visibility was not assessed. An observed entry may be pre-existing
    or partial; visibility establishes neither admitted bytes nor durability.
    False does not mean the directory is empty: an older record or staged write
    may remain. For archival, the target is the history copy; its visibility
    establishes no active-slot release. Reconcile failures before dispatch.
    """

    def __init__(
        self,
        message,
        *,
        cause=None,
        record_visible=None,
        cleanup_failures=(),
        removed_history_records=(),
        uncertain_history_record=None,
        history_directory_synced=None,
        history_trim_phase=None,
    ):
        super().__init__(message)
        self.cause = cause
        self.record_visible = record_visible
        self.cleanup_failures = tuple(cleanup_failures)
        self.removed_history_records = tuple(removed_history_records)
        self.uncertain_history_record = uncertain_history_record
        self.history_directory_synced = history_directory_synced
        self.history_trim_phase = history_trim_phase


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
    confirmed_history_records: int = field(kw_only=True)

    def __post_init__(self):
        if type(self.maximum_records) is not int or self.maximum_records < 1:
            raise ValueError("Cannot configure Attempt store: maximum_records must be a positive integer")
        if type(self.confirmed_history_records) is not int or self.confirmed_history_records < 1:
            raise ValueError("Cannot configure Attempt store: confirmed_history_records must be an explicit positive integer")
        if type(self.recovery_record_bytes) is not int or self.recovery_record_bytes < MAX_RECORD_BYTES:
            raise ValueError(
                "Cannot configure Attempt store: recovery_record_bytes must cover at least the lifecycle-record limit"
            )
        if type(self.filesystem_reserve_bytes) is not int or self.filesystem_reserve_bytes < 0:
            raise ValueError("Cannot configure Attempt store: filesystem reserve must be a nonnegative byte count")


def record_name(record):
    # Body admission already owns Job/key grammar. The digest is a filename, not
    # proof of body integrity or permission to recover across scopes.
    key = json.loads(pending_start(record).body)["provider_attempt_key"]
    return sha256(key.encode("ascii")).hexdigest() + ".json"


def history_directory_name(scope):
    """Bind a history-only root to its original scope without exposing its labels.

    The name is local recovery metadata, not authentication of historical bytes.
    Each archive that is actually accessed still undergoes original-file admission.
    """
    raw = json.dumps(asdict(scope), sort_keys=True, separators=(",", ":")).encode("ascii")
    return "history-" + sha256(raw).hexdigest()


class AttemptFiles:
    def __init__(self, root, scope, limits):
        self.root = root
        self.scope = scope
        self.limits = limits
        self.fd = None
        self.history_fd = None
        self.history_name = history_directory_name(scope)
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
            # Stopped recovery needs active obligations, not unrelated old
            # payloads. Expiry later admits every historical byte before unlink.
            self._history_names(self.limits.confirmed_history_records + self.limits.maximum_records)
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
        failures = []
        for attribute in ("history_fd", "fd"):
            descriptor = getattr(self, attribute)
            if descriptor is not None:
                setattr(self, attribute, None)
                try:
                    self._close_descriptor(descriptor)
                except OSError as cause:
                    failures.append(cause)
        if failures:
            raise StoreFailed(
                "Cannot close Attempt storage directories; enclosing process retirement is required",
                cause=failures[0],
                cleanup_failures=failures[1:],
            ) from None
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

    def _read(self, name, *, directory):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
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
            if metadata.st_size > MAX_RECORD_BYTES:
                raise ValueError("Retained record exceeds the lifecycle-record byte limit")
            chunks, total = [], 0
            while total <= MAX_RECORD_BYTES:
                chunk = os.read(descriptor, min(65536, MAX_RECORD_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            raw = b"".join(chunks)
            record = decode_record(raw, scope=self.scope)
            if record_name(record) != name:
                raise ValueError("Retained filename differs from the provider attempt key")
            os.fsync(descriptor)
            return record, raw
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

    def _open_history(self, *, create):
        if self.history_fd is not None:
            return self.history_fd
        if create:
            with suppress(FileExistsError):
                os.mkdir(self.history_name, 0o700, dir_fd=self.fd)
        self.history_fd = os.open(self.history_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.fd)
        metadata = os.fstat(self.history_fd)
        if (
            metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_dev != os.fstat(self.fd).st_dev
        ):
            raise ValueError("Confirmed history must be private, owner-controlled and on the same filesystem")
        if create:
            # Retire no active entry before the history namespace itself is durable.
            os.fsync(self.fd)
        return self.history_fd

    def _history_record(self, name):
        if self.history_fd is None:
            return None
        try:
            record, raw = self._read(name, directory=self.history_fd)
            if type(record) is not TerminalConfirmed:
                raise ValueError("Historical record has no matching terminal receipt")
            return record, raw
        except FileNotFoundError:
            return None
        except (OSError, ValueError, StoreFailed) as cause:
            self.poisoned = True
            raise StoreFailed(
                "Cannot inspect confirmed history; preserve its files and original active records", cause=cause
            ) from None

    def _history_names(self, maximum):
        if self.history_fd is None:
            return []
        found = []
        try:
            with os.scandir(self.history_fd) as entries:
                for entry in entries:
                    if re.fullmatch(r"[0-9a-f]{64}\.json", entry.name) is None:
                        raise ValueError("Unknown or unfinished confirmed-history entry requires stopped inspection")
                    found.append(entry.name)
                    if len(found) > maximum:
                        raise StoreRefused(
                            "Confirmed history exceeds its configured record window; preserve the files for stopped inspection, without automatic migration or expiry"
                        )
        except (OSError, ValueError) as cause:
            self.poisoned = True
            raise StoreFailed(
                "Cannot enumerate confirmed history; preserve every record before admission or expiry", cause=cause
            ) from None
        return found

    def _history_inventory(self):
        # Decode one original record at a time; retain only compact ordering keys,
        # never all historical scientific payloads in memory together.
        maximum = self.limits.confirmed_history_records + self.limits.maximum_records
        inventory = []
        for name in self._history_names(maximum):
            record, _raw = self._history_record(name)
            inventory.append((record.receipt.committed_at, name))
            del record, _raw
        return sorted(inventory)

    def require_confirmed_history_capacity(self):
        self._usable()
        self._history_names(self.limits.confirmed_history_records)

    def _inventory(self):
        self._usable()
        found = []
        try:
            with os.scandir(self.fd) as entries:
                for entry in entries:
                    if entry.name == self.history_name:
                        self._open_history(create=False)
                        continue
                    if re.fullmatch(r"[0-9a-f]{64}\.json", entry.name) is None:
                        raise ValueError("Unexpected or unfinished staging entry requires explicit recovery")
                    found.append(entry.name)
                    if len(found) > self.limits.maximum_records:
                        raise ValueError("Retained inventory exceeds configured record slots")
            inventory = []
            for name in sorted(found):
                record, raw = self._read(name, directory=self.fd)
                archived = self._history_record(name)
                if archived is not None and (type(record) is not TerminalConfirmed or archived != (record, raw)):
                    raise ValueError("Active record and confirmed history are not identical confirmed bytes")
                inventory.append((name, record, len(raw)))
            return inventory
        except (OSError, ValueError, StoreFailed) as cause:
            self.poisoned = True
            raise StoreFailed(
                "Cannot read Attempt inventory; retained data is unsafe or unavailable; preserve it for recovery", cause=cause
            ) from None

    def records(self):
        return tuple(record for _name, record, _size in self._inventory())

    def admission_inventory(self):
        inventory = self._inventory()
        self._history_names(self.limits.confirmed_history_records)
        self._require_new_record_capacity(inventory)
        return tuple(record for _name, record, _size in inventory)

    def admit(self, record):
        self._usable()
        if type(record) is not StartPending:
            raise StoreRefused(
                "New storage admission requires a pending start; later states require replacement of the retained record"
            )
        try:
            raw = encode_record(record)
        except ValueError as cause:
            raise StoreRefused(str(cause)) from None
        if record.scope != self.scope:
            raise StoreRefused("Cannot retain pending start under a different recovery scope")
        target = record_name(record)
        inventory = self._inventory()
        self._history_names(self.limits.confirmed_history_records)
        if self._history_record(target) is not None:
            raise StoreRefused("Provider attempt key already has archived confirmation; it cannot become a fresh start")
        for name, existing, _size in inventory:
            if name == target:
                if existing != record:
                    raise StoreRefused(
                        "Provider attempt key already has different retained facts; preserve the original obligation"
                    )
                return existing
        self._require_new_record_capacity(inventory)
        self._publish(target, raw, replacing=False)
        return record

    def replace(self, expected, replacement):
        self._usable()
        try:
            require_transition(expected, replacement)
            raw = encode_record(replacement)
        except ValueError as cause:
            # These checks ran before filesystem effects; report a determinate
            # refusal, not an uncertain storage failure. Their text is locally owned.
            raise StoreRefused(str(cause)) from None
        if pending_start(expected).scope != self.scope:
            raise StoreRefused("Cannot replace Attempt record under a different recovery scope")
        target = record_name(expected)
        inventory = self._inventory()
        current = next((record for name, record, _size in inventory if name == target), None)
        if current != expected:
            raise StoreRefused(
                "Cannot replace Attempt record: retained state differs from the expected record; reread before deciding what to do"
            )
        self._require_capacity(inventory, additional_records=0)
        self._publish(target, raw, replacing=True)
        return replacement

    def archive_confirmed(self, expected):
        """Preserve exact confirmed bytes before releasing their active slot.

        This storage operation grants no lifecycle release or native-retirement
        authority. Composition chooses it after those original owners settle.
        Partial copies remain evidence; only an identical complete copy can finish
        interrupted archival. No history or unresolved record is deleted.
        """
        self._usable()
        if type(expected) is not TerminalConfirmed or pending_start(expected).scope != self.scope:
            raise StoreRefused("Only an exact confirmed record in this scope can be archived")
        target = record_name(expected)
        inventory = self._inventory()
        current = next((record for name, record, _size in inventory if name == target), None)
        archived = self._history_record(target)
        if current is None:
            if archived is None or archived[0] != expected:
                raise StoreRefused("Cannot archive: the expected confirmation is absent from active storage and history")
        elif current != expected:
            raise StoreRefused("Cannot archive: active storage differs from the expected confirmation")
        elif archived is None:
            maximum = self.limits.confirmed_history_records + self.limits.maximum_records
            if len(self._history_names(maximum)) >= maximum:
                raise StoreRefused(
                    "Cannot archive another confirmed record beyond the bounded transition window; preserve history for stopped inspection"
                )
            self._require_capacity(inventory, additional_records=0)
        descriptor = None
        visible, durable, removed = archived is not None, False, current is None
        try:
            directory = self._open_history(create=current is not None)
            if archived is None:
                actual, raw = self._read(target, directory=self.fd)
                if actual != expected:
                    raise ValueError("Original record changed before its archive copy")
                # A direct exclusive leaf avoids introducing a second staging
                # namespace or relaxing ordinary singly-linked record admission.
                descriptor = os.open(
                    target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory
                )
                visible = True
                self._write_bytes(descriptor, raw)
                closing, descriptor = descriptor, None
                self._close_descriptor(closing)
            os.fsync(directory)
            durable = True
            if current is not None:
                os.unlink(target, dir_fd=self.fd)
                removed = True
            os.fsync(self.fd)
            return expected
        except BaseException as cause:
            self.poisoned = True
            cleanup = []
            if descriptor is not None:
                closing, descriptor = descriptor, None
                try:
                    self._close_descriptor(closing)
                except OSError as error:
                    cleanup.append(error)
            if not durable:
                message = "Confirmed history copy is unconfirmed; preserve the original active record and any history file"
            elif not removed:
                message = "Confirmed history copy is durable, but active-entry removal is unconfirmed; preserve both locations"
            else:
                message = (
                    "History is durable and active-entry removal was observed, but active-directory sync is unconfirmed; "
                    "preserve history and reconcile storage before reuse"
                )
            raise StoreFailed(message, cause=cause, record_visible=visible, cleanup_failures=cleanup) from None

    def trim_confirmed_history(self):
        """Expire complete oldest records after composition's native release ACK.

        The store owns only its private confirmed history. Composition must first
        settle native lifetimes; no active obligation may pass this expiry gate.
        """
        self._usable()
        if self._inventory():
            raise StoreRefused(
                "Cannot expire confirmed history while active Attempt records remain; archive the clean inventory first"
            )
        removed, target, phase = [], None, "validation"
        try:
            inventory = self._history_inventory()
            expired = inventory[: -self.limits.confirmed_history_records]
            if not expired:
                return 0
            phase = "unlink"
            for _committed_at, target in expired:
                os.unlink(target, dir_fd=self.history_fd)
                removed.append(target)
            target = None
            phase = "directory_sync"
            os.fsync(self.history_fd)
            return len(removed)
        except BaseException as cause:
            self.poisoned = True
            raise StoreFailed(
                "Confirmed-history expiry is unconfirmed; preserve remaining records and the observed removal facts before any admission",
                cause=cause,
                removed_history_records=removed,
                uncertain_history_record=target,
                history_directory_synced=False,
                history_trim_phase=phase,
            ) from None

    def _require_new_record_capacity(self, inventory):
        if len(inventory) >= self.limits.maximum_records:
            raise StoreRefused(
                "Cannot admit a fresh Attempt: all retained-record slots are occupied; finished Attempts also count. "
                "Preserve the records. Stop and settle the provider before increasing maximum_records "
                "with enough disk space and restarting."
            )
        self._require_capacity(inventory, additional_records=1)

    def _require_capacity(self, inventory, *, additional_records):
        try:
            space = os.fstatvfs(self.fd)
            # Existing on-disk bytes already consume free space. Reserve arithmetic
            # covers their future growth, any new full record and one full staging
            # record; unrelated writers can still invalidate this observation.
            required = (
                (len(inventory) + additional_records + 1) * self.limits.recovery_record_bytes
                - sum(size for _name, _record, size in inventory)
                + self.limits.filesystem_reserve_bytes
            )
            if space.f_bavail * space.f_frsize < required:
                raise StoreRefused(
                    "Cannot retain Attempt record: insufficient filesystem headroom for full obligations and staged replacement"
                )
        except OSError as cause:
            self.poisoned = True
            raise StoreFailed(
                "Cannot check Attempt recovery capacity; no record was installed", cause=cause, record_visible=False
            ) from None

    def _publish(self, target, raw, *, replacing):
        staging = ".pending-" + secrets.token_hex(16)
        descriptor = None
        visible = False
        try:
            descriptor = os.open(
                staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.fd
            )
            self._write_bytes(descriptor, raw)
            closing, descriptor = descriptor, None
            self._close_descriptor(closing)
            if replacing:
                os.replace(staging, target, src_dir_fd=self.fd, dst_dir_fd=self.fd)
                visible = True
            else:
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
                "Cannot confirm durable Attempt retention; preserve storage and reconcile before further API or scientific effects",
                cause=cause,
                record_visible=visible,
                cleanup_failures=cleanup,
            ) from None

    def _write_bytes(self, descriptor, raw):
        """Fill and sync one newly owned file; the operation retains close authority."""
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(raw):
            count = os.write(descriptor, raw[offset:])
            if count <= 0:
                raise StoreFailed("Cannot retain record bytes: the owned file write made no progress")
            offset += count
        os.fsync(descriptor)
