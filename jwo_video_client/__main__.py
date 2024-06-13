import asyncio
import logging
import signal
import sys

import aiohttp
import aiortc
import tomllib
from aiortc.contrib import media

CONFIG_PATH = "jwo_video_client/config.toml"

logging.basicConfig()
logger = logging.Logger("jwo_video_client")


def create_video_track(
    dev_idx: int, size: str, frame_rate: int
) -> media.MediaStreamTrack:
    device_node = f"/dev/video{dev_idx}"
    options = {
        "video_size": size,
        "framerate": str(frame_rate),
    }
    player = media.MediaPlayer(device_node, format="v4l2", options=options)

    media_relay = media.MediaRelay()
    return media_relay.subscribe(player.video)


def create_rtc_peer_connection(
    video_track: media.MediaStreamTrack,
) -> aiortc.RTCPeerConnection:
    connection = aiortc.RTCPeerConnection()

    @connection.on("connectionstatechange")
    async def on_connection_state_change():
        logger.info("Connection state is %s", connection.connectionState)
        if connection.connectionState == "failed":
            await connection.close()

    connection.addTrack(video_track)
    return connection


async def send_rtc_conn_offer(
    connection: aiortc.RTCPeerConnection, server_url: str
) -> None:
    offer = await connection.createOffer()
    await connection.setLocalDescription(offer)

    offer_signal_body = {"sdp": offer.sdp, "type": offer.type}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(server_url, json=offer_signal_body) as resp:
                resp = await resp.json()
                answer = aiortc.RTCSessionDescription(
                    sdp=resp["sdp"], type=resp["type"]
                )
    except Exception as exc:
        await connection.close()
        raise exc

    connection.setRemoteDescription(answer)


def main():
    with open(CONFIG_PATH, "rb") as file:
        config = tomllib.load(file)

    video_config = config["video"]
    video_track = create_video_track(
        video_config["dev_idx"], video_config["image_size"], video_config["frame_rate"]
    )

    webrtc_config = config["webrtc"]
    rtc_peer_connection = create_rtc_peer_connection(video_track)
    asyncio.run(send_rtc_conn_offer(rtc_peer_connection, webrtc_config["server"]))

    def sigint_handler():
        logger.info("Shutting down...")
        sys.exit(0)

    signal.signal(signal.SIGINT, sigint_handler)


if __name__ == "__main__":
    main()
