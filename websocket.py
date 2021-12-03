from __future__ import annotations

import json
import random
import string
import asyncio
import logging
from typing import Any, Optional, Dict, Tuple, Set, Iterable, TYPE_CHECKING

from websockets.exceptions import ConnectionClosed, ConnectionClosedOK
from websockets.client import WebSocketClientProtocol, connect as websocket_connect
from exceptions import MinerException

from inventory import TimedDrop
from constants import WEBSOCKET_URL, PING_INTERVAL, WebsocketTopic, get_topic

if TYPE_CHECKING:
    from twitch import Twitch


logger = logging.getLogger("TwitchDrops")


class Websocket:
    def __init__(self, twitch: Twitch):
        self._twitch = twitch
        self._ws: Optional[WebSocketClientProtocol] = None
        self.connected = asyncio.Event()  # set when there's an active websocket connection
        self.reconnect = asyncio.Event()  # set when the websocket needs to reconnect
        self._send_queue: asyncio.Queue[Tuple[str, Dict[str, Any]]] = asyncio.Queue()
        self._recv_dict: Dict[str, asyncio.Future[Any]] = {}
        self._topics: Set[WebsocketTopic] = set()
        self._ping_task: Optional[asyncio.Task[Any]] = None

    async def connect(self):
        # ensure we're logged in before connecting
        await self._twitch.wait_until_login()
        assert self._twitch._user_id is not None
        logger.info("Connecting to Websocket")
        # Listen to our events of choice
        user_id = self._twitch._user_id
        # Defer topics via a task so we can proceed to the actual websocket connection
        asyncio.create_task(
            self.add_topics([
                get_topic("UserDrops", user_id, self.process_drops),
                get_topic("UserCommunityPoints", user_id, self.process_points),
            ])
        )
        # Connect/Reconnect loop
        async for websocket in websocket_connect(WEBSOCKET_URL, ssl=True, ping_interval=None):
            websocket.BACKOFF_MAX = 3 * 60  # type: ignore  # 3 minutes
            self._ws = websocket
            self.reconnect.clear()
            self.connected.set()
            logger.info("Websocket Connected")
            self._ping_task = asyncio.create_task(self._ping_loop())
            try:
                while not self.reconnect.is_set():
                    # Process receive
                    try:
                        # Wait up to 0.5s for a message we're supposed to receive
                        raw_message = await asyncio.wait_for(websocket.recv(), timeout=0.5)
                    except asyncio.TimeoutError:
                        # nothing - skip handling
                        pass
                    else:
                        # we've got something to process
                        message = json.loads(raw_message)
                        logger.debug(f"Websocket received: {message}")
                        msg_type = message["type"]
                        # handle the simple PING case
                        if msg_type == "PONG":
                            ping_future = self._recv_dict.pop("PING", None)
                            if ping_future is not None and not ping_future.done():
                                ping_future.set_result(message)
                        elif msg_type == "RESPONSE":
                            self._recv_dict.pop(message["nonce"]).set_result(message)
                        elif msg_type == "RECONNECT":
                            # We've received a reconnect request
                            logger.warning("Received a Websocket Reconnect Request")
                            self.reconnect.set()
                        elif msg_type == "MESSAGE":
                            # request the assigned topic to process the response
                            target_topic = message["data"]["topic"]
                            for topic in self._topics:
                                if target_topic == topic:
                                    await topic.process(json.loads(message["data"]["message"]))
                                    break
                        else:
                            logger.error(f"Received unknown websocket payload: {message}")

                    # Early exit if needed
                    if self.reconnect.is_set():
                        break

                    # Process send
                    while not self._send_queue.empty():
                        nonce, message = self._send_queue.get_nowait()
                        if nonce != "PING":
                            message["nonce"] = nonce
                        await websocket.send(json.dumps(message, separators=(',', ':')))
                        logger.debug(f"Websocket sent: {message}")
                # A reconnect was requested
                continue
            except ConnectionClosed as exc:
                self.connected.clear()
                if self._ping_task is not None:
                    self._ping_task.cancel()
                    self._ping_task = None
                if isinstance(exc, ConnectionClosedOK):
                    if exc.rcvd_then_sent:
                        # server closed the connection, not us - reconnect
                        logger.warning("Websocket Disconnected - Reconnecting")
                        continue
                    # we closed it - exit
                    break
                # otherwise, reconnect
                logger.warning("Websocket Closed - Reconnecting")
                continue
            except Exception:
                logger.exception("Exception in Websocket - Reconnecting")
                continue

    async def close(self):
        self.connected.clear()
        if self._ping_task is not None:
            self._ping_task.cancel()
        if self._ws is not None:
            await self._ws.close()

    async def _ping_loop(self):
        await self.connected.wait()
        ping_every = PING_INTERVAL.total_seconds()
        while self.connected.is_set():
            try:
                await asyncio.wait_for(self.send({"type": "PING"}), timeout=10)
            except asyncio.TimeoutError:
                # per documentation, if there's no response for a PING, reconnect to the websocket
                logger.warning("Websocket got no response to PING - reconnect")
                self.reconnect.set()
                break
            await asyncio.sleep(ping_every)

    def create_nonce(self, length: int = 30) -> str:
        available_chars = string.ascii_letters + string.digits
        return ''.join(random.choices(available_chars, k=length))

    def send(self, message: Dict[str, Any]) -> asyncio.Future[Dict[str, Any]]:
        logger.debug(f"Websocket sending: {message}")
        msg_type = message["type"]
        if msg_type == "PING":
            nonce = "PING"
        else:
            nonce = self.create_nonce()
        self._send_queue.put_nowait((nonce, message))
        future: asyncio.Future[Dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._recv_dict[nonce] = future
        return future

    async def add_topics(self, topics: Iterable[WebsocketTopic]):
        # ensure no topics end up duplicated
        topics = set(topics)
        topics.difference_update(self._topics)
        if not topics:
            # none left to add
            return
        self._topics.update(topics)
        if len(self._topics) >= 50:
            # TODO: Handle multiple connections (up to 10) since one allows only up to 50 topics
            raise MinerException("Too many topics")
        await self.connected.wait()
        assert self._twitch._access_token is not None
        auth_token = self._twitch._access_token
        topics_list = list(map(str, topics))
        logger.info(f"Listening for: {', '.join(topics_list)}")
        await self.send(
            {
                "type": "LISTEN",
                "data": {
                    "topics": topics_list,
                    "auth_token": auth_token,
                }
            }
        )

    async def process_drops(self, message: Dict[str, Any]):
        drop_id = message["data"]["drop_id"]
        drop: Optional[TimedDrop] = None
        for campaign in self._twitch.inventory:
            drop = campaign.get_drop(drop_id)
            if drop is not None:
                break
        else:
            logger.error(f"Drop with ID of {drop_id} not found!")
            return
        drop.update(message)
        msg_type = message["type"]
        if msg_type == "drop-progress":
            print(
                f"Drop progress: {drop.progress:4.0%} ({drop.remaining_minutes} minutes remaining)"
            )
        elif msg_type == "drop-claim":
            await drop.claim()
            print(f"Claimed drop: {', '.join(drop.rewards)}")

    async def process_points(self, message: Dict[str, Any]):
        msg_type = message["type"]
        if msg_type == "points-earned":
            points = message["data"]["point_gain"]["total_points"]
            balance = message["data"]["balance"]["balance"]
            print(f"Earned points for watching: {points:3}, total: {balance}")
        elif msg_type == "claim-available":
            claim_data = message["data"]["claim"]
            points = claim_data["point_gain"]["total_points"]
            await self._twitch.claim_points(claim_data["channel_id"], claim_data["id"])
            print(f"Claimed bonus points: {points}")