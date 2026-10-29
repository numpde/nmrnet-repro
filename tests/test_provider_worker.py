"""Real process/socket boundaries, with recording science rather than released models."""

from tools.container_guard import require_container

require_container()

import asyncio
import base64
import hashlib
import io
import json
import multiprocessing
import socket
import struct
import tempfile
import traceback
import unittest
from contextlib import suppress
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from tests.test_prediction_input import PredictionDictionary, encoded_request

from nmrnet_inference import model as prediction_model
from nmrnet_inference.predict import MAX_INPUT_BYTES
from nmrnet_inference.release import ReleasedPredictionSession, SelectedReleaseModels
from nmrnet_inference.worker import serve_connection
from nmrnet_provider.worker_client import (
    ModelCallFailed,
    PredictionClient,
    PredictionExecutionFailed,
    WorkerExchangeTimedOut,
    WorkerReportedFailure,
    WorkerResultRejected,
)
from nmrnet_provider.worker_protocol import MAX_FRAME_BYTES, WorkerProtocolError, document, frame
from nmrnet_provider.process_reports import failure_evidence


class RecordingPrediction:
    declaration_sha256 = "d" * 64

    def __init__(self, entered=None, release=None):
        self.entered, self.release = entered, release

    def prepare(self, raw):
        self.handle = object()
        self.raw = raw
        return self.handle

    def execute(self, handle):
        if handle is not self.handle:
            raise AssertionError("foreign scientific handle")
        if self.entered is not None:
            self.entered.set()
            self.release.wait(2)
        return tuple({"preserved_bytes": raw.hex()} for raw in self.raw)


def run_worker(connection, other_end, entered=None, release=None):
    other_end.close()
    # This fixture deliberately disconnects while science is busy. The real
    # owner must retain stderr; suppressing the broken reply is test-local.
    with connection, suppress(BrokenPipeError, EOFError):
        serve_connection(connection, RecordingPrediction(entered, release), allocation="test-allocation", io_seconds=1)


class PredictionWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_worker_changed_declaration_cannot_enter_prediction(self):
        context = multiprocessing.get_context("fork")
        entered, release = context.Event(), context.Event()
        parent, child = socket.socketpair()
        worker = context.Process(target=run_worker, args=(child, parent, entered, release))
        worker.start()
        child.close()
        reader, writer = await asyncio.open_unix_connection(sock=parent)
        client = PredictionClient(reader, writer, allocation="test-allocation", operation_seconds=1,
                                  declaration_sha256="e" * 64)
        try:
            with self.assertRaisesRegex(WorkerProtocolError, "model declaration"):
                await client.prepare((b"request",))
            self.assertFalse(entered.is_set())
        finally:
            release.set()
            client.close()
            await writer.wait_closed()
            await asyncio.to_thread(worker.join, 2)
            if worker.is_alive():
                worker.kill()
                await asyncio.to_thread(worker.join)

    async def test_real_worker_returns_the_whole_ordered_batch_once(self):
        parent, child = socket.socketpair()
        worker = multiprocessing.get_context("fork").Process(target=run_worker, args=(child, parent))
        worker.start()
        child.close()
        reader, writer = await asyncio.open_unix_connection(sock=parent)
        client = PredictionClient(reader, writer, allocation="test-allocation", operation_seconds=1, declaration_sha256="d" * 64)
        try:
            requests = (b"first", b"different", b"first")
            handle = await client.prepare(requests)
            result = await client.execute(handle)
            self.assertEqual(tuple({"preserved_bytes": raw.hex()} for raw in requests), result)
            with self.assertRaises(ValueError):
                await client.execute(handle)
        finally:
            client.close()
            await client.wait_closed()
            await asyncio.to_thread(worker.join, 2)
            if worker.is_alive():
                worker.kill()
                await asyncio.to_thread(worker.join)
            self.assertEqual(worker.exitcode, 0)

    async def test_local_close_timeout_retains_original_observation(self):
        release = asyncio.Event()
        writer = Mock()
        writer.wait_closed = AsyncMock(side_effect=release.wait)
        client = PredictionClient(None, writer, allocation="test-allocation", operation_seconds=0.01, declaration_sha256="d" * 64)
        client.close()
        with self.assertRaises(TimeoutError):
            await client.wait_closed()
        original = client._close_waiter
        self.assertFalse(original.done())
        release.set()
        await client.wait_closed()
        self.assertIs(original, client._close_waiter)
        self.assertEqual(writer.wait_closed.await_count, 1)

    async def test_real_worker_preserves_input_and_consumes_local_handle(self):
        parent, child = socket.socketpair()
        worker = multiprocessing.get_context("fork").Process(target=run_worker, args=(child, parent))
        worker.start()
        child.close()
        reader, writer = await asyncio.open_unix_connection(sock=parent)
        client = PredictionClient(reader, writer, allocation="test-allocation", operation_seconds=1, declaration_sha256="d" * 64)
        try:
            raw = b' {"exact": "source"}\n'
            handle = await client.prepare((raw,))
            with self.assertRaises(ValueError):
                await client.execute(object())
            result = await client.execute(handle)
            self.assertEqual(result, ({"preserved_bytes": raw.hex()},))
            with self.assertRaises(ValueError):
                await client.execute(handle)
        finally:
            client.close()
            await client.wait_closed()
            await asyncio.to_thread(worker.join, 2)
            if worker.is_alive():
                worker.kill()
                await asyncio.to_thread(worker.join)
            self.assertEqual(worker.exitcode, 0)

    async def test_cancellation_closes_client_but_does_not_stop_busy_worker(self):
        context = multiprocessing.get_context("fork")
        entered, release = context.Event(), context.Event()
        parent, child = socket.socketpair()
        worker = context.Process(target=run_worker, args=(child, parent, entered, release))
        worker.start()
        child.close()
        reader, writer = await asyncio.open_unix_connection(sock=parent)
        client = PredictionClient(reader, writer, allocation="test-allocation", operation_seconds=1, declaration_sha256="d" * 64)
        try:
            handle = await client.prepare((b"request",))
            task = asyncio.create_task(client.execute(handle))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(worker.is_alive())
            with self.assertRaises(ValueError):
                await client.prepare((b"replacement",))
        finally:
            release.set()
            client.close()
            await writer.wait_closed()
            await asyncio.to_thread(worker.join, 2)
            if worker.is_alive():
                worker.kill()
                await asyncio.to_thread(worker.join)
            self.assertEqual(worker.exitcode, 0)


class RecordingConnection:
    def __init__(self, incoming, failure=None):
        self.incoming = io.BytesIO(incoming)
        self.sent = []
        self.failure = failure

    def settimeout(self, seconds):
        pass

    def recv(self, size):
        return self.incoming.read(min(size, 3))

    def sendall(self, raw):
        self.sent.append(raw)
        if self.failure is not None and len(self.sent) == 2:
            raise self.failure


def prepare_frame(raw=b"request", allocation="test-allocation"):
    return frame({"operation": "prepare", "allocation": allocation, "requests_base64": [base64.b64encode(raw).decode("ascii")]})


def execute_frame(token):
    return frame({"operation": "execute", "allocation": "test-allocation", "token": token})


class WorkerFramingTests(unittest.TestCase):
    def test_model_failure_has_a_distinct_private_frame(self):
        token = "a" * 32
        connection = RecordingConnection(prepare_frame() + execute_frame(token))
        prediction = RecordingPrediction()
        cause = prediction_model.ModelCallFailed("model failure")
        with (patch("nmrnet_inference.worker.secrets.token_hex", return_value=token),
              patch.object(prediction, "execute", side_effect=cause)):
            serve_connection(connection, prediction, allocation="test-allocation", io_seconds=1)
        reply = document(connection.sent[-1][4:])
        self.assertEqual(reply["kind"], "model_call_failed")
        self.assertIn("model failure", reply["diagnostic"])

    def test_later_geometry_failure_sends_cause_and_completed_input_identity_without_values(self):
        requests = (encoded_request(atoms=["H"], coordinates=[[0, 0, 0]]), encoded_request(atoms=["C"], coordinates=[[0, 0, 0]]))
        token = "a" * 32
        connection = RecordingConnection(
            frame(
                {
                    "operation": "prepare",
                    "allocation": "test-allocation",
                    "requests_base64": [base64.b64encode(raw).decode() for raw in requests],
                }
            )
            + execute_frame(token)
        )
        models = Mock(
            declaration_sha256="d" * 64,
            load_dictionary=Mock(return_value=PredictionDictionary()),
            load_selected_element=Mock(return_value=(object(), object())),
            observed_artifacts=Mock(return_value={"dictionary": {}, "elements": {}}),
            observed_warnings=Mock(return_value=[]),
        )
        session = ReleasedPredictionSession(models)
        cause = RuntimeError("PRIVATE_LATER_GEOMETRY_CAUSE")
        with (
            patch("nmrnet_inference.worker.secrets.token_hex", return_value=token),
            patch("nmrnet_inference.release.importlib.metadata.version", return_value="fixture"),
            patch.object(prediction_model, "compose_net_input", return_value=object()),
            patch.object(prediction_model, "predict_composed_element", side_effect=[((0, 987654.321),), cause]),
        ):
            serve_connection(connection, session, allocation="test-allocation", io_seconds=1)
        replies = [document(raw[4:]) for raw in connection.sent]
        self.assertEqual(["prepared", "execution_failed"], [reply["kind"] for reply in replies])
        self.assertNotIn("result", replies[-1])
        diagnostic = replies[-1]["diagnostic"]
        self.assertIn("PRIVATE_LATER_GEOMETRY_CAUSE", diagnostic)
        self.assertIn("completed_geometries", diagnostic)
        self.assertIn('"failed_geometry": 2', diagnostic)
        self.assertNotIn('"shift_ppm"', diagnostic)
        self.assertNotIn("987654.321", diagnostic)

    def test_fragmented_frames_preserve_maximum_input(self):
        raw = b" " * MAX_INPUT_BYTES
        token = "a" * 32
        connection = RecordingConnection(prepare_frame(raw) + execute_frame(token))
        prediction = RecordingPrediction()
        # A maximal input need not produce an equally large result. Keep this
        # witness about byte-preserving admission rather than hex expansion.
        with (
            patch("nmrnet_inference.worker.secrets.token_hex", return_value=token),
            patch.object(prediction, "execute", return_value={"accepted": True}),
        ):
            serve_connection(connection, prediction, allocation="test-allocation", io_seconds=2)
        self.assertEqual(prediction.raw, (raw,))

    def test_failed_result_send_never_attempts_a_second_frame(self):
        token = "a" * 32
        cause = OSError("result was partially sent")
        connection = RecordingConnection(prepare_frame() + execute_frame(token), failure=cause)
        prediction = RecordingPrediction()
        with (
            patch("nmrnet_inference.worker.secrets.token_hex", return_value=token),
            patch.object(prediction, "execute", return_value=({"shift_ppm": 987654.321},)),
            self.assertRaises(OSError) as failed,
        ):
            serve_connection(connection, prediction, allocation="test-allocation", io_seconds=1)
        self.assertIs(failed.exception, cause)
        self.assertEqual(len(connection.sent), 2)
        self.assertIn('"result_delivery": "unconfirmed"', cause.__notes__[-1])
        self.assertNotIn("987654.321", "".join(traceback.format_exception(cause)))

    def test_oversized_success_reply_reports_unconfirmed_delivery_without_payload(self):
        token = "a" * 32
        connection = RecordingConnection(prepare_frame() + execute_frame(token))
        prediction = RecordingPrediction()
        outcomes = ({"private_result": "PRIVATE_SCIENTIFIC_RESULT" + "x" * MAX_FRAME_BYTES},)
        with (
            patch("nmrnet_inference.worker.secrets.token_hex", return_value=token),
            patch.object(prediction, "execute", return_value=outcomes),
            self.assertRaises(WorkerProtocolError) as failure,
        ):
            serve_connection(connection, prediction, allocation="test-allocation", io_seconds=1)
        self.assertEqual(1, len(connection.sent))
        self.assertNotIn("PRIVATE_SCIENTIFIC_RESULT", str(failure.exception))
        self.assertNotIn("PRIVATE_SCIENTIFIC_RESULT", repr(failure.exception))
        self.assertNotIn("PRIVATE_SCIENTIFIC_RESULT", failure.exception.__notes__[-1])
        self.assertNotIn("PRIVATE_SCIENTIFIC_RESULT", "".join(traceback.format_exception(failure.exception)))
        self.assertIn('"result_delivery": "unconfirmed"', failure.exception.__notes__[-1])

    def test_replacement_invalidates_previous_wire_token(self):
        connection = RecordingConnection(prepare_frame() + prepare_frame(b"replacement") + execute_frame("a" * 32))
        with patch("nmrnet_inference.worker.secrets.token_hex", side_effect=["a" * 32, "b" * 32]):
            serve_connection(connection, RecordingPrediction(), allocation="test-allocation", io_seconds=1)
        response = document(connection.sent[-1][4:])
        self.assertEqual(response["kind"], "failure")
        self.assertIn("current preparation token", response["diagnostic"])

    def test_oversized_truncated_and_wrong_allocation_frames_never_prepare(self):
        for raw, error in (
            (struct.pack("!I", MAX_FRAME_BYTES + 1), WorkerProtocolError),
            (b"\0\0", EOFError),
            (struct.pack("!I", 10) + b"{}", EOFError),
            (prepare_frame(allocation="foreign"), WorkerProtocolError),
        ):
            with self.subTest(raw=raw[:10]), self.assertRaises(error):
                serve_connection(RecordingConnection(raw), object(), allocation="test-allocation", io_seconds=1)

    def test_nonfinite_and_duplicate_json_are_rejected(self):
        for raw in (b'{"x":NaN}', b'{"x":1e999}', b'{"x":1,"x":2}'):
            with self.subTest(raw=raw), self.assertRaises(WorkerProtocolError):
                document(raw)


class BufferedWriter:
    def __init__(self):
        self.sent = []
        self.closed = False

    def write(self, raw):
        self.sent.append(raw)

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class ClientFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_declaration_reader_is_bound_through_worker_to_client(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            declaration = root / "models.json"
            original = b'{"selection":"first"}'
            declaration.write_bytes(original)
            expected = hashlib.sha256(original).hexdigest()
            for changed in (False, True):
                with self.subTest(changed=changed):
                    if changed:
                        declaration.write_bytes(b'{"selection":"changed"}')
                    models = SelectedReleaseModels(declaration_path=declaration, weight_root=root, checkpoint_root=root)
                    prediction = ReleasedPredictionSession(models)
                    prediction._prediction.prepare = Mock(side_effect=lambda _requests: (models._selection(), object())[1])
                    connection = RecordingConnection(prepare_frame())
                    with (patch("nmrnet_inference.worker.secrets.token_hex", return_value="a" * 32),
                          patch("nmrnet_inference.release.importlib.metadata.version", return_value="fixture"),
                          suppress(EOFError)):
                        serve_connection(connection, prediction, allocation="test-allocation", io_seconds=1)
                    client, writer = self.client(connection.sent[0], declaration_sha256=expected)
                    if changed:
                        with self.assertRaisesRegex(WorkerProtocolError, "model declaration"):
                            await client.prepare((b"request",))
                        self.assertTrue(writer.closed)
                    else:
                        self.assertIsNotNone(await client.prepare((b"request",)))
                    self.assertEqual(len(writer.sent), 1)

    async def test_changed_model_declaration_blocks_prediction_before_execute(self):
        reply = frame({"kind": "prepared", "allocation": "test-allocation", "token": "a" * 32,
                       "declaration_sha256": "e" * 64})
        client, writer = self.client(reply)
        with self.assertRaisesRegex(WorkerProtocolError, "this worker's prediction was not dispatched") as failure:
            await client.prepare((b"request",))
        rendered = json.dumps(failure_evidence(failure.exception, secrets=()))
        self.assertIn("model declaration differs", rendered)
        self.assertNotIn("e" * 64, rendered)
        self.assertTrue(writer.closed)
        self.assertEqual(len(writer.sent), 1)

    async def test_model_failure_frame_is_distinct_from_general_execution_failure(self):
        replies = frame({"kind": "prepared", "allocation": "test-allocation", "token": "a" * 32, "declaration_sha256": "d" * 64}) + frame(
            {"kind": "model_call_failed", "allocation": "test-allocation", "diagnostic": "private cause"}
        )
        client, writer = self.client(replies)
        handle = await client.prepare((b"request",))
        with self.assertRaises(ModelCallFailed) as caught:
            await client.execute(handle)
        self.assertEqual(caught.exception.diagnostic, "private cause")
        self.assertTrue(writer.closed)

    async def test_wrong_result_count_or_shape_preserves_private_reply(self):
        for result in ([], [{"private": "PRIVATE_RESULT"}] * 2, {"private": "PRIVATE_RESULT"}):
            with self.subTest(shape=type(result).__name__):
                replies = frame({"kind": "prepared", "allocation": "test-allocation", "token": "a" * 32, "declaration_sha256": "d" * 64}) + frame(
                    {"kind": "result", "allocation": "test-allocation", "result": result}
                )
                client, writer = self.client(replies)
                handle = await client.prepare((b"request",))
                with self.assertRaises(WorkerResultRejected) as failure:
                    await client.execute(handle)
                self.assertEqual(result, json.loads(failure.exception.diagnostic)["result"])
                self.assertNotIn("PRIVATE_RESULT", str(failure.exception))
                self.assertNotIn("PRIVATE_RESULT", repr(failure.exception))
                self.assertTrue(writer.closed)

    async def test_aggregate_raw_size_is_rejected_before_encoding_any_request(self):
        client, writer = self.client(b"")
        requests = (b"x" * MAX_INPUT_BYTES,) * 3
        with patch("nmrnet_provider.worker_client.base64.b64encode") as encode, self.assertRaises(ValueError):
            await client.prepare(requests)
        encode.assert_not_called()
        self.assertEqual([], writer.sent)

    async def test_base64_and_json_frame_overflow_never_writes_a_partial_batch(self):
        client, writer = self.client(b"")
        with self.assertRaises(WorkerProtocolError):
            await client.prepare((b"x" * MAX_INPUT_BYTES,) * 2)
        self.assertEqual([], writer.sent)

    async def test_batch_shape_and_count_are_rejected_before_encoding(self):
        for requests in ((), (b"request",) * 129, [b"request"], ("request",)):
            with self.subTest(shape=type(requests).__name__):
                client, writer = self.client(b"")
                with (
                    patch("nmrnet_provider.worker_client.base64.b64encode") as encode,
                    self.assertRaises((TypeError, ValueError)),
                ):
                    await client.prepare(requests)
                encode.assert_not_called()
                self.assertEqual([], writer.sent)

    def client(self, reply_bytes, seconds=1, declaration_sha256="d" * 64):
        reader = asyncio.StreamReader()
        reader.feed_data(reply_bytes)
        reader.feed_eof()
        writer = BufferedWriter()
        return PredictionClient(reader, writer, allocation="test-allocation", operation_seconds=seconds,
                                declaration_sha256=declaration_sha256), writer

    async def test_oversized_input_is_rejected_before_base64_encoding(self):
        client, writer = self.client(b"")
        with patch("nmrnet_provider.worker_client.base64.b64encode") as encode, self.assertRaises(ValueError):
            await client.prepare((b"x" * (MAX_INPUT_BYTES + 1),))
        encode.assert_not_called()
        self.assertEqual(writer.sent, [])

    async def test_invalid_reply_shape_allocation_token_and_prefix_close_channel(self):
        for reply_bytes in (
            frame({"kind": "prepared", "allocation": "foreign", "token": "a" * 32, "declaration_sha256": "d" * 64}),
            frame({"kind": "prepared", "allocation": "test-allocation", "token": "", "declaration_sha256": "d" * 64}),
            struct.pack("!I", MAX_FRAME_BYTES + 1),
            b"\0\0",
        ):
            client, writer = self.client(reply_bytes)
            with self.assertRaises((WorkerProtocolError, asyncio.IncompleteReadError)):
                await client.prepare((b"request",))
            self.assertTrue(writer.closed)

    async def test_private_failure_is_retained_but_not_rendered(self):
        client, writer = self.client(frame({"kind": "failure", "allocation": "test-allocation", "diagnostic": "private cause"}))
        with self.assertRaises(WorkerReportedFailure) as failed:
            await client.prepare((b"request",))
        self.assertEqual(failed.exception.diagnostic, "private cause")
        self.assertNotIn("private cause", str(failed.exception))
        self.assertTrue(writer.closed)

    async def test_admitted_execution_failure_is_distinct_from_generic_worker_refusal(self):
        for kind, error in (("execution_failed", PredictionExecutionFailed), ("failure", WorkerReportedFailure)):
            with self.subTest(kind=kind):
                replies = frame({"kind": "prepared", "allocation": "test-allocation", "token": "a" * 32, "declaration_sha256": "d" * 64})
                replies += frame({"kind": kind, "allocation": "test-allocation", "diagnostic": "private cause"})
                client, writer = self.client(replies)
                handle = await client.prepare((b"request",))
                with self.assertRaises(error) as caught:
                    await client.execute(handle)
                self.assertIs(type(caught.exception), error)
                self.assertEqual(caught.exception.diagnostic, "private cause")
                self.assertNotIn("private cause", str(caught.exception))
                self.assertTrue(writer.closed)

    async def test_owned_exchange_deadline_is_distinct_from_a_foreign_timeout(self):
        reader, writer = asyncio.StreamReader(), BufferedWriter()
        client = PredictionClient(reader, writer, allocation="test-allocation", operation_seconds=0.01, declaration_sha256="d" * 64)
        with self.assertRaises(WorkerExchangeTimedOut):
            await client.prepare((b"request",))
        foreign = TimeoutError("Authored independent timeout")
        client, writer = self.client(b"")
        with patch.object(writer, "drain", side_effect=foreign), self.assertRaises(TimeoutError) as caught:
            await client.prepare((b"request",))
        self.assertIs(caught.exception, foreign)

    async def test_dependency_timeout_after_timer_cancellation_keeps_its_identity(self):
        foreign = TimeoutError("Independent timeout after cancellation")

        async def drain():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise foreign from None

        client, writer = self.client(b"", seconds=0.01)
        with patch.object(writer, "drain", side_effect=drain), self.assertRaises(TimeoutError) as caught:
            await client.prepare((b"request",))
        self.assertIs(caught.exception, foreign)

    async def test_elapsed_buffered_reply_deadline_does_not_return_a_handle(self):
        reply_bytes = frame({"kind": "prepared", "allocation": "test-allocation", "token": "a" * 32, "declaration_sha256": "d" * 64})
        client, writer = self.client(reply_bytes)
        loop = asyncio.get_running_loop()
        current = loop.time()
        with (
            patch.object(loop, "time", side_effect=[current, current, current, current + 2]),
            self.assertRaises(WorkerExchangeTimedOut),
        ):
            await client.prepare((b"request",))
        self.assertTrue(writer.closed)

    async def test_partial_reply_timeout_closes_channel_without_retry(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b"\0\0")
        writer = BufferedWriter()
        client = PredictionClient(reader, writer, allocation="test-allocation", operation_seconds=0.01, declaration_sha256="d" * 64)
        with self.assertRaises(TimeoutError):
            await client.prepare((b"request",))
        self.assertTrue(writer.closed)
        self.assertEqual(len(writer.sent), 1)
        with self.assertRaises(ValueError):
            await client.prepare((b"request",))

    async def test_explicit_close_cannot_return_a_late_prepared_handle(self):
        reader = asyncio.StreamReader()
        writer = BufferedWriter()
        client = PredictionClient(reader, writer, allocation="test-allocation", operation_seconds=1, declaration_sha256="d" * 64)
        task = asyncio.create_task(client.prepare((b"request",)))
        await asyncio.sleep(0)
        client.close()
        reader.feed_data(frame({"kind": "prepared", "allocation": "test-allocation", "token": "a" * 32, "declaration_sha256": "d" * 64}))
        with self.assertRaises(WorkerProtocolError):
            await task
        with self.assertRaises(ValueError):
            await client.execute(object())

    async def test_rejected_replacement_revokes_previous_handle_without_sending(self):
        client, writer = self.client(frame({"kind": "prepared", "allocation": "test-allocation", "token": "a" * 32, "declaration_sha256": "d" * 64}))
        handle = await client.prepare((b"original",))
        with self.assertRaises(ValueError):
            await client.prepare((b"x" * (MAX_INPUT_BYTES + 1),))
        with self.assertRaises(ValueError):
            await client.execute(handle)
        self.assertEqual(len(writer.sent), 1)
