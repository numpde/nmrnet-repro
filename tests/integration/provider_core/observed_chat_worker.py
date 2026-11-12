"""Observe admitted model response headers inside the isolated SDK worker."""

import sys
from pathlib import Path
from unittest.mock import patch

from provider_core.chat_worker import main
from provider_core.transport import HttpTransport


def received_head(_head):
    (Path(sys.argv[1]) / "model-head-received").write_bytes(b"observed\n")


async def observed_exchange(transport, request, **arguments):
    return await exchange(transport, request, before_body=received_head, **arguments)


if __name__ == "__main__":
    exchange = HttpTransport.exchange_decoded
    with patch.object(HttpTransport, "exchange_decoded", observed_exchange):
        raise SystemExit(main())
