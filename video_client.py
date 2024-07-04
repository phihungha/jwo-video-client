import argparse
import asyncio
import logging

import aiohttp
import aiohttp.web
import aiortc
import cv2
import tomllib
from aiortc.contrib import media

CONFIG_PATH = "config.toml"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jwo_video_client")

media_relay = media.MediaRelay()
media_blackhole = media.MediaBlackhole()


class VideoDisplayTrack(aiortc.MediaStreamTrack):
    kind = "video"

    def __init__(
        self, track: aiortc.MediaStreamTrack, video_conn: aiortc.RTCPeerConnection
    ):
        super().__init__()
        self.track = track
        self.video_conn = video_conn

    async def recv(self):
        video_frame = await self.track.recv()
        cv2.imshow("Debug", video_frame.to_ndarray(format="bgr24"))

        if cv2.waitKey(1) == ord("q"):
            cv2.destroyWindow("Debug")
            logger.info("Closing video connection...")
            await self.video_conn.close()
            exit(0)

        return video_frame


def create_video_track(
    dev_idx: int, size: str, frame_rate: int
) -> aiortc.MediaStreamTrack:
    """Create video track from a capture device.

    Args:
        dev_idx (int): Device index
        size (str): Image size
        frame_rate (int): Video frame rate

    Returns:
        aiortc.MediaStreamTrack: Video track
    """

    device_node = f"/dev/video{dev_idx}"
    options = {
        "video_size": size,
        "framerate": str(frame_rate),
    }
    player = media.MediaPlayer(device_node, format="v4l2", options=options)

    return media_relay.subscribe(player.video)


def create_video_conn(
    video_track: aiortc.MediaStreamTrack, accept_debug_video: bool
) -> aiortc.RTCPeerConnection:
    """Create a WebRTC peer connection to stream provided video track.

    Args:
        video_track (aiortc.MediaStreamTrack): Video track

    Returns:
        aiortc.RTCPeerConnection: WebRTC peer connection
    """

    peer_conn = aiortc.RTCPeerConnection()

    @peer_conn.on("connectionstatechange")
    async def on_conn_state_change():
        logger.info("Connection state is %s", peer_conn.connectionState)
        if peer_conn.connectionState == "failed":
            await peer_conn.close()

    @peer_conn.on("track")
    def on_track(track: aiortc.MediaStreamTrack):
        if track.kind != "video":
            return

        logger.info("Received debug video track.")
        display_track = VideoDisplayTrack(media_relay.subscribe(track), peer_conn)
        media_blackhole.addTrack(display_track)
        cv2.namedWindow("Debug")

    if accept_debug_video:
        peer_conn.addTransceiver(video_track)
    else:
        peer_conn.addTransceiver(video_track, direction="sendonly")

    return peer_conn


async def send_video_conn_offer(
    peer_conn: aiortc.RTCPeerConnection, server_url: str, use_debug_video: bool
) -> None:
    offer = await peer_conn.createOffer()
    await peer_conn.setLocalDescription(offer)

    offer_body = {
        "sdp": offer.sdp,
        "type": offer.type,
        "use_debug_video": use_debug_video,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(server_url, json=offer_body) as resp:
            resp = await resp.json()

    answer = aiortc.RTCSessionDescription(sdp=resp["sdp"], type=resp["type"])
    await peer_conn.setRemoteDescription(answer)


async def main(server_url: str, debug_mode: bool):
    video_config = config["video"]
    video_track = create_video_track(
        video_config["dev_idx"], video_config["image_size"], video_config["frame_rate"]
    )
    video_conn = create_video_conn(video_track, accept_debug_video=True)

    server_url = config["video_server"]["url"]
    await send_video_conn_offer(video_conn, server_url, debug_mode)

    await media_blackhole.start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="JWO Video Client",
        description="Video client for the Just-Walk-Out Shopping System.",
    )
    parser.add_argument("-d", "--debug")
    args = parser.parse_args()

    with open(CONFIG_PATH, "rb") as file:
        config = tomllib.load(file)

    asyncio.run(main(config, debug_mode=True))
