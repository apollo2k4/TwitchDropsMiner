from __future__ import annotations

import os
import asyncio
import logging
from yarl import URL
from getpass import getpass
from functools import partial
from typing import Any, Optional, Union, List, Dict, cast

try:
    import aiohttp
except ImportError:
    raise ImportError("You have to run 'python -m pip install aiohttp' first")

from channel import Channel
from websocket import Websocket
from inventory import DropsCampaign
from exceptions import LoginException, CaptchaRequired, IncorrectCredentials
from constants import (
    CLIENT_ID,
    USER_AGENT,
    COOKIES_PATH,
    GQL_URL,
    GQL_OPERATIONS,
    GQLOperation,
    WebsocketTopic,
    get_topic,
)


logger = logging.getLogger("TwitchDrops")


class Twitch:
    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        *,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        self.username: Optional[str] = username
        self.password: Optional[str] = password
        # Cookies, session and auth
        cookie_jar = aiohttp.CookieJar()
        if os.path.isfile(COOKIES_PATH):
            cookie_jar.load(COOKIES_PATH)
        self._session = aiohttp.ClientSession(
            cookie_jar=cookie_jar, headers={"User-Agent": USER_AGENT}, loop=loop
        )
        self._access_token: Optional[str] = None
        self._user_id: Optional[int] = None
        self._is_logged_in = asyncio.Event()
        # Websocket
        self.websocket = Websocket(self)
        # Storing, watching and changing channels
        self.channels: Dict[int, Channel] = {}
        self._watching_channel: Optional[Channel] = None
        self._watching_task: Optional[asyncio.Task[Any]] = None
        self._channel_change = asyncio.Event()
        # Inventory
        self.inventory: List[DropsCampaign] = []

    def wait_until_login(self):
        return self._is_logged_in.wait()

    async def close(self):
        print("Exiting...")
        self._session.cookie_jar.save(COOKIES_PATH)  # type: ignore
        self.stop_watching()
        await self._session.close()
        await self.websocket.close()
        await asyncio.sleep(1)  # allows aiohttp to safely close the session

    @property
    def currently_watching(self) -> Optional[Channel]:
        return self._watching_channel

    async def run(self, channels: List[str] = []):
        """
        Main method that runs the whole client.

        Here, we manage several things, specifically:
        • Fetching the drops inventory to make sure that everything we can claim, is claimed
        • Selecting a stream to watch, and watching it
        • Changing the stream that's being watched if necessary
        """
        # Start our websocket connection - shouldn't require task tracking
        asyncio.create_task(self.websocket.connect())
        # Claim the drops we can
        self.inventory = await self.get_inventory()
        games = set()
        for campaign in self.inventory:
            if campaign.status == "UPCOMING":
                # we have no use in processing upcoming campaigns here
                continue
            for drop in campaign.timed_drops.values():
                if drop.can_earn:
                    games.add(campaign.game)
                if drop.can_claim:
                    await drop.claim()
        # Fetch information about all channels we're supposed to handle
        for channel_name in channels:
            channel: Channel = await Channel(self, channel_name)  # type: ignore
            self.channels[channel.id] = channel
        # Sub to these channel updates
        topics: List[WebsocketTopic] = []
        for channel_id in self.channels:
            topics.append(
                get_topic(
                    "VideoPlayback", channel_id, partial(self.process_stream_state, channel_id)
                )
            )
        await self.websocket.add_topics(topics)

        # Repeat: Change into a channel we can watch, then reset the flag
        self._channel_change.set()
        refresh_channels = False  # we're entering having fresh channel data already
        while True:
            # wait for the change channel signal
            await self._channel_change.wait()
            for channel in self.channels.values():
                if (
                    channel.stream is not None  # steam online
                    and channel.stream.drops_enabled  # drops are enabled
                    and channel.stream.game in games  # streams a game we can earn drops in
                ):
                    self.watch(channel)
                    refresh_channels = True
                    self._channel_change.clear()
                    break
            else:
                # there's no available channel to watch
                if refresh_channels:
                    # refresh the status of all channels,
                    # to make sure that our websocket didn't miss anything til this point
                    print("No suitable channel to watch, refreshing...")
                    for channel in self.channels.values():
                        await channel.get_stream()
                        await asyncio.sleep(0.5)
                    refresh_channels = False
                    continue
                print("No suitable channel to watch, retrying in 120 seconds")
                await asyncio.sleep(120)

    def watch(self, channel: Channel):
        if self._watching_task is not None:
            self._watching_task.cancel()

        async def watcher(channel: Channel):
            op = GQL_OPERATIONS["ChannelPointsContext"].with_variables(
                {"channelLogin": channel.name}
            )
            i = 0
            while True:
                await channel._send_watch()
                if i == 0:
                    # ensure every 30 minutes that we don't have unclaimed points bonus
                    response = await self.gql_request(op)
                    channel_data: Dict[str, Any] = response["data"]["community"]["channel"]
                    claim_available: Dict[str, Any] = (
                        channel_data["self"]["communityPoints"]["availableClaim"]
                    )
                    if claim_available:
                        await self.claim_points(channel_data["id"], claim_available["id"])
                        logger.info("Claimed bonus points")
                i = (i + 1) % 30
                await asyncio.sleep(59)

        print(f"Watching: {channel.name}")
        self._watching_channel = channel
        self._watching_task = asyncio.create_task(watcher(channel))

    def stop_watching(self):
        if self._watching_task is not None:
            logger.warning("Watching stopped.")
            self._watching_task.cancel()
            self._watching_task = None
        self._watching_channel = None

    async def process_stream_state(self, channel_id: int, message: Dict[str, Any]):
        msg_type = message["type"]
        channel = self.channels.get(channel_id)
        if channel is None:
            logger.error(f"Stream state change for a non-existing channel: {channel_id}")
            return
        if msg_type == "stream-down":
            logger.info(f"{channel.name} goes OFFLINE")
            channel.set_offline()
            if self._watching_channel is not None and self._watching_channel.id == channel_id:
                # change the channel if we're currently watching it
                self._channel_change.set()
        elif msg_type == "stream-up":
            logger.info(f"{channel.name} goes ONLINE")

            # stream_up is sent before the stream actually goes online, so just wait a bit
            # and check if it's actually online by then
            async def online_delay(channel: Channel):
                await asyncio.sleep(10)
                await channel.check_online()

            asyncio.create_task(online_delay(channel))
        elif msg_type == "viewcount":
            if channel.stream is None:
                # check if we've got a view count for a stream that just started
                await channel.check_online()
            if channel.stream is not None:
                viewers = message["viewers"]
                channel.stream.viewer_count = viewers
                logger.info(f"{channel.name} viewers: {viewers}")
            else:
                logger.error(f"Channel viewcount update for an offline stream: {channel.name}")

    async def _login(self) -> str:
        logger.debug("Login flow started")
        if self.username is None:
            self.username = input("Username: ")
        if self.password is None:
            self.password = getpass()
        if not self.password:
            # catch early empty pass
            raise IncorrectCredentials()

        payload: Dict[str, Any] = {
            "username": self.username,
            "password": self.password,
            "client_id": CLIENT_ID,
            "undelete_user": False,
            "remember_me": True,
        }

        for attempt in range(10):
            async with self._session.post(
                "https://passport.twitch.tv/login", json=payload
            ) as response:
                login_response = await response.json()

            # Feed this back in to avoid running into CAPTCHA if possible
            if "captcha_proof" in login_response:
                payload["captcha"] = {"proof": login_response["captcha_proof"]}

            # Error handling
            if "error_code" in login_response:
                error_code = login_response["error_code"]
                logger.debug(f"Login error code: {error_code}")
                if error_code == 1000:
                    # we've failed bois
                    logger.debug("Login failed due to CAPTCHA")
                    raise CaptchaRequired()
                elif error_code == 3001:
                    # wrong password you dummy
                    logger.debug("Login failed due to incorrect login or pass")
                    print(f"Incorrect username or password.\nUsername: {self.username}")
                    self.password = getpass()
                    if not self.password:
                        raise IncorrectCredentials()
                elif error_code in (
                    3011,  # Authy token needed
                    3012,  # Invalid authy token
                    3022,  # Email code needed
                    3023,  # Invalid email code
                ):
                    # 2FA handling
                    email = error_code in (3022, 3023)
                    logger.debug("2FA token required")
                    token = input("2FA token: ")
                    if email:
                        # email code
                        payload["twitchguard_code"] = token
                    else:
                        # authy token
                        payload["authy_token"] = token
                    continue
                else:
                    raise LoginException(login_response["error"])

            # Success handling
            if "access_token" in login_response:
                # we're in bois
                self._access_token = login_response["access_token"]
                logger.debug(f"Access token: {self._access_token}")
                break

        if self._access_token is None:
            # this means we've ran out of retries
            raise LoginException("Ran out of login retries")
        return self._access_token

    async def check_login(self) -> None:
        if self._access_token is not None and self._user_id is not None:
            # we're all good
            return
        # looks like we're missing something
        print("Logging in")
        jar = self._session.cookie_jar
        cookie = jar.filter_cookies("https://twitch.tv")  # type: ignore
        if not cookie:
            # no cookie - login
            await self._login()
            # store our auth token inside the cookie
            cookie["auth-token"] = cast(str, self._access_token)
        elif self._access_token is None:
            # have cookie - get our access token
            self._access_token = cookie["auth-token"].value
            logger.debug("Session restored from cookie")
        # validate our access token, by obtaining user_id
        async with self._session.get(
            "https://id.twitch.tv/oauth2/validate",
            headers={"Authorization": f"OAuth {self._access_token}"}
        ) as response:
            validate_response = await response.json()
        self._user_id = cookie["persistent"] = validate_response["user_id"]
        self._is_logged_in.set()
        print(f"Login successful, User ID: {self._user_id}")
        # update our cookie
        jar.update_cookies(cookie, URL("https://twitch.tv"))

    async def gql_request(self, op: GQLOperation) -> Dict[str, Any]:
        await self.check_login()
        headers = {
            "Authorization": f"OAuth {self._access_token}",
            "Client-Id": CLIENT_ID,
        }
        logger.debug(f"GQL Request: {op}")
        async with self._session.post(GQL_URL, json=op, headers=headers) as response:
            response_json = await response.json()
            logger.debug(f"GQL Response: {response_json}")
            return response_json

    async def get_inventory(self) -> List[DropsCampaign]:
        response = await self.gql_request(GQL_OPERATIONS["Inventory"])
        inventory = response["data"]["currentUser"]["inventory"]
        return [DropsCampaign(self, data) for data in inventory["dropCampaignsInProgress"]]

    async def claim_points(self, channel_id: Union[str, int], claim_id: str):
        variables = {"input": {"channelID": str(channel_id), "claimID": claim_id}}
        await self.gql_request(
            GQL_OPERATIONS["ClaimCommunityPoints"].with_variables(variables)
        )