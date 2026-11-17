import asyncio

from app.services.key_device_counts import device_counts


class API:
    def __init__(self, payload=None, error=False):
        self.payload, self.error = payload, error

    async def devices(self, uuid):
        if self.error:
            raise RuntimeError("offline")
        return self.payload


def device(hwid):
    return dict(
        hwid=hwid,
        userId=123,
        platform=None,
        osVersion=None,
        deviceModel=None,
        userAgent=None,
        requestIp=None,
        createdAt="2026-10-01T00:00:00Z",
        updatedAt="2026-10-01T00:00:00Z",
    )


def test_authoritative_counts_and_unavailable():
    result = asyncio.run(
        device_counts(
            [
                (
                    1,
                    API(
                        {
                            "response": {
                                "total": 2,
                                "devices": [device("a"), device("b")],
                            }
                        }
                    ),
                    "123",
                ),
                (2, API(error=True), "123"),
                (3, API({"response": {}}), "123"),
                (4, API({"response": {"total": 0, "devices": []}}), "123"),
                (
                    5,
                    API({"response": {"total": 2, "devices": [device("a"), {}]}}),
                    "123",
                ),
            ]
        )
    )
    assert result == {1: 2, 2: None, 3: None, 4: 0, 5: None}
