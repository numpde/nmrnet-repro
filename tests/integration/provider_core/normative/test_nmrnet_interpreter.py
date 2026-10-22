"""Local scripted HTTPS interpretation reaches each actual input/result route."""

import hashlib
import json
import unittest

from tests.fakes.tls_peer import TlsPeer
from tests.fakes.uploads import UPLOAD_REF
from tests.integration.nmrnet.interpreter_ports import (
    UploadPorts,
    chat_endpoint,
    tool_reply,
)
from tests.integration.nmrnet.pasted_ports import PastedPorts
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.interpreter import PROMPT, InputInterpreter
from provider_core.upload_response import UploadMember, UploadSet


class InterpreterRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_shift_precision_is_advertised_and_frozen_in_one_exchange(self):
        ports = SessionPorts()

        async def respond(_headers, raw):
            request = json.loads(raw)
            self.assertIn("4 significant digits", request["messages"][0]["content"])
            for tool in request["tools"]:
                function = tool["function"]
                if function["name"].startswith("prepare_"):
                    parameters = function["parameters"]
                    self.assertEqual(
                        parameters["properties"]["shift_significant_digits"],
                        {"type": "integer", "minimum": 1, "maximum": 17, "default": 4},
                    )
                    self.assertNotIn("shift_significant_digits", parameters["required"])
            return tool_reply("prepare", "prepare_smiles", {"smiles": "CCl", "requested_count": 2, "shift_significant_digits": 3})

        async with TlsPeer("success", response_factory=respond) as peer:
            chooser = InputInterpreter((chat_endpoint(peer),), max_turns=2)
            session = ports.make_session(ports.owner(chooser.run, chooser=chooser))
            self.addAsyncCleanup(session.retire)
            handle = await session.prepare(ports.acknowledged, ports.instructions)
            self.assertEqual(json.loads(handle.identity.content)["output"], {"shift_significant_digits": 3})
            result = json.loads((await session.execute(handle)).content)
            self.assertEqual(result["output"], {"shift_significant_digits": 3})
            self.assertEqual(result["interpretation"]["operations"], ["prepare_smiles"])
            self.assertEqual(result["interpretation"]["turns_started"], 1)
            self.assertEqual(result["interpretation"]["corrections"], 0)
            ports.generator.generate.assert_awaited_once_with(b"CCl", 2)
            self.assertEqual(len(peer.request_bodies), 1)

    async def test_lost_reply_before_dispatch_falls_back_and_prepares_once(self):
        ports = SessionPorts()

        async def first_reply(_headers, _raw):
            return None

        async def second_reply(_headers, _raw):
            return tool_reply("prepare", "prepare_smiles", {"smiles": "CCl", "requested_count": 2})

        async with (
            TlsPeer("success", response_factory=first_reply) as first_peer,
            TlsPeer("success", response_factory=second_reply) as second_peer,
        ):
            endpoints = (chat_endpoint(first_peer, identifier="first"), chat_endpoint(second_peer, identifier="second"))
            chooser = InputInterpreter(endpoints, max_turns=2)
            session = ports.make_session(ports.owner(chooser.run, chooser=chooser))
            self.addAsyncCleanup(session.retire)
            handle = await session.prepare(ports.acknowledged, ports.instructions)
            ports.generator.generate.assert_awaited_once_with(b"CCl", 2)
            self.assertEqual(ports.worker.events, [])
            self.assertTrue(all(endpoint.retirement_confirmed for endpoint in endpoints))
            observation = json.loads(handle.identity.content)["interpretation"]
            self.assertEqual([r["turns_started"] for r in observation["routes"]], [1, 1])
            outcome = await session.execute(handle)
            self.assertEqual(json.loads(outcome.content)["interpretation"], observation)
            self.assertEqual(ports.worker.events, ["prepare", "execute"])

    async def test_three_interpreted_routes_reach_prediction_with_bound_provenance(
        self,
    ):
        for route in ("smiles", "pasted", "upload"):
            with self.subTest(route=route):
                if route == "pasted":
                    ports = PastedPorts(b"H 0.125 0 0\n", source_format="table", source_unit="nm")
                elif route == "upload":
                    ports = UploadPorts()
                else:
                    ports = SessionPorts()
                ports.uploads.inspect.return_value = UploadSet(
                    "job:example",
                    (UploadMember(UPLOAD_REF, "Authored XYZ source", len(ports.source), None),),
                )

                async def respond(_headers, raw, route=route, ports=ports):
                    request = json.loads(raw)
                    messages = request["messages"]
                    last = messages[-1]
                    if route == "smiles":
                        return tool_reply(
                            "prepare",
                            "prepare_smiles",
                            {"smiles": "CCl", "requested_count": 2},
                        )
                    if route == "pasted":
                        return tool_reply(
                            "prepare",
                            "prepare_pasted",
                            {
                                "source_text": ports.source_text,
                                "source_format": "table",
                                "source_unit": "nm",
                            },
                        )
                    if last["role"] != "tool":
                        return tool_reply("inspect", "inspect_uploads", {})
                    return tool_reply(
                        "prepare",
                        "prepare_upload",
                        {
                            "upload_ref": UPLOAD_REF,
                            "source_format": "xyz",
                            "source_unit": "angstrom",
                            "selection": [2, 1],
                            "alternate_location": None,
                        },
                    )

                async with TlsPeer("success", response_factory=respond) as peer:
                    endpoint = chat_endpoint(peer)
                    chooser = InputInterpreter((endpoint,), max_turns=5)
                    owner = ports.owner(chooser.run, chooser=chooser)
                    session = ports.make_session(owner)
                    self.addAsyncCleanup(session.retire)
                    handle = await session.prepare(ports.acknowledged, ports.instructions)
                    self.assertEqual(ports.worker.events, [])
                    self.assertTrue(endpoint.retirement_confirmed)
                    prepared_identity = json.loads(handle.identity.content)
                    outcome = await session.execute(handle)
                    result = json.loads(outcome.content)
                    self.assertEqual(result["output"], {"shift_significant_digits": 4})
                    self.assertEqual(result["interpretation"], prepared_identity["interpretation"])
                    self.assertEqual(result["interpretation"]["outcome"], "selected")
                    interpretation = result["interpretation"]
                    self.assertEqual(
                        interpretation["prompt_sha256"],
                        hashlib.sha256(PROMPT.encode()).hexdigest(),
                    )
                    requests = [json.loads(raw) for raw in peer.request_bodies]
                    self.assertEqual(len(requests), 2 if route == "upload" else 1)
                    self.assertNotIn("submit", [tool["function"]["name"] for tool in requests[0]["tools"]])
                    tools = json.dumps(
                        requests[0]["tools"],
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                    self.assertEqual(
                        interpretation["tools_sha256"],
                        hashlib.sha256(tools).hexdigest(),
                    )
                    self.assertEqual(interpretation["turns_started"], len(requests))
                    self.assertEqual(
                        interpretation["routes"],
                        [
                            {
                                "endpoint": json.loads(endpoint.identity),
                                "turns_started": len(requests),
                            }
                        ],
                    )
                    for private in (
                        b"PRIVATE_MODEL_KEY",
                        b"fixture reasoning",
                        peer.origin.encode(),
                        b"PRIVATE_GENERATOR_DIAGNOSTIC",
                    ):
                        self.assertNotIn(private, json.dumps(result["interpretation"]).encode())
                    self.assertEqual(len(ports.worker.requests), 1 if route == "pasted" else 2)
                    if route == "smiles":
                        ports.generator.generate.assert_awaited_once_with(b"CCl", 2)
                        self.assertEqual(
                            [g["source"]["conformer_id"] for g in result["geometries"]],
                            [2, 0],
                        )
                    elif route == "pasted":
                        self.assertEqual(
                            json.loads(ports.worker.requests[0])["coordinates"],
                            [[1.25, 0.0, 0.0]],
                        )
                        geometry = result["geometries"][0]
                        self.assertEqual(geometry["source"]["origin"], "interpreter_supplied_coordinates")
                        self.assertEqual(geometry["preparation"]["source_unit"], "nm")
                    else:
                        ports.uploads.acquire.assert_awaited_once_with(UPLOAD_REF)
                        self.assertEqual(ports.reader.read.call_args.args, (ports.source,))
                        self.assertEqual(ports.reader.read.call_args.kwargs["selection"], (2, 1))
                        self.assertEqual(
                            [json.loads(raw)["atoms"] for raw in ports.worker.requests],
                            [["H"], ["O"]],
                        )
                        self.assertEqual(
                            [g["source"]["record_number"] for g in result["geometries"]],
                            [2, 1],
                        )
                        self.assertTrue(
                            all(g["source"]["sha256"] == hashlib.sha256(ports.source).hexdigest() for g in result["geometries"])
                        )
                    await session.retire()
