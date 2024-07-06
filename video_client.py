import argparse
import asyncio
import logging
import signal

import aiohttp
import aiohttp.client_exceptions
import aiohttp.web
import aiortc
import cv2
import tomllib
from aiortc.contrib import media

CONFIG_PATH = "config.toml"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s|%(levelname)s] %(message)s",
)
logger = logging.getLogger("jwo_video_client")

media_relay = media.MediaRelay()
media_blackhole = media.MediaBlackhole()


class AppException(Exception):
    pass


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
            asyncio.get_event_loop().stop()

        return video_frame


def create_video_track_from_capture_dev(
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


def create_video_track_from_file(
    file_path: str, size: str, frame_rate: int
) -> aiortc.MediaStreamTrack:
    """Create video track from a video file.

    Args:
        file_path (str): File path
        size (str): Image size
        frame_rate (int): Video frame rate

    Returns:
        aiortc.MediaStreamTrack: Video track
    """

    options = {
        "video_size": size,
        "framerate": str(frame_rate),
    }
    player = media.MediaPlayer(file_path, options=options)

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

    event_loop = asyncio.get_event_loop()

    async def on_stop_signal():
        logger.info("Video connection is being shut down...")
        await peer_conn.close()
        event_loop.stop()

    for sig_name in ("SIGINT", "SIGTERM"):
        event_loop.add_signal_handler(
            getattr(signal, sig_name), lambda: asyncio.create_task(on_stop_signal())
        )

    @peer_conn.on("connectionstatechange")
    async def on_conn_state_change():
        logger.info("Connection state is %s", peer_conn.connectionState)
        if peer_conn.connectionState == "failed":
            await peer_conn.close()

    @peer_conn.on("track")
    def on_track(track: aiortc.MediaStreamTrack):
        if track.kind != "video":
            return
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
) -> str:
    """Send offer to setup WebRTC peer connection for video stream.

    Args:
        peer_conn (aiortc.RTCPeerConnection): Peer connection
        server_url (str): Server URL
        use_debug_video (bool): Request return debug video

    Returns:
        str: Client ID assigned by server
    """

    offer = await peer_conn.createOffer()
    await peer_conn.setLocalDescription(offer)

    offer_body = {
        "sdp": offer.sdp,
        "type": offer.type,
        "use_debug_video": use_debug_video,
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(server_url, json=offer_body) as resp:
                resp = await resp.json()
        except aiohttp.client_exceptions.ClientConnectionError as err:
            raise AppException(f"Failed to connect to {server_url}") from err

    answer = aiortc.RTCSessionDescription(sdp=resp["sdp"], type=resp["type"])
    await peer_conn.setRemoteDescription(answer)

    return resp["id"]


async def main(server_url: str, args: argparse.Namespace):
    video_config = config["video"]
    video_file_path = args.file

    if video_file_path is not None:
        video_track = create_video_track_from_file(
            video_file_path, video_config["image_size"], video_config["frame_rate"]
        )
    else:
        video_track = create_video_track_from_capture_dev(
            video_config["dev_idx"],
            video_config["image_size"],
            video_config["frame_rate"],
        )

    video_conn = create_video_conn(video_track, accept_debug_video=True)

    server_url = config["video_server"]["url"]
    await send_video_conn_offer(video_conn, server_url, args.debug)

    await media_blackhole.start()


def exception_handler(event_loop, context):
    """https://stackoverflow.com/questions/43207927/how-to-shutdown-the-loop-and-print-error-if-coroutine-raised-an-exception-with-a"""

    exception = context.get("exception")
    if isinstance(exception, AppException):
        logger.error(exception)
    else:
        logger.exception(exception)

    event_loop.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="JWO Video Client",
        description="Video client for the Just-Walk-Out Shopping System.",
    )
    parser.add_argument("-d", "--debug", action="store_true")
    parser.add_argument("-f", "--file")
    args = parser.parse_args()

    with open(CONFIG_PATH, "rb") as file:
        config = tomllib.load(file)

    event_loop = asyncio.new_event_loop()
    event_loop.set_exception_handler(exception_handler)
    asyncio.set_event_loop(event_loop)

    event_loop.create_task(main(config, args))
    event_loop.run_forever()
