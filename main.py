# pyright: strict

import argparse
import json
import os
import pathlib as pl
import re
import shutil
import signal
import tempfile
import threading
import urllib.parse
from typing import IO, Any, Callable, Final, cast

import pydantic as pyd
import tinytag
import tornado.httpserver
import tornado.ioloop
import tornado.web
import tornado.websocket
import yt_dlp

# optional libs
try:
    # Chromecast support
    import pychromecast.discovery
except ModuleNotFoundError:
    pychromecast = None
try:
    # Spotify "support"
    import spotify_scraper
    import youtube_search  # pyright: ignore[reportMissingTypeStubs]
except ModuleNotFoundError:
    spotify_scraper = None
    youtube_search = None

# local libs
import _config
import _types
import connections
import mp3Juggler

loop: tornado.ioloop.IOLoop | None = None
clients: connections.Connections | None = None
juggler: mp3Juggler.Juggler | None = None
http_server: tornado.httpserver.HTTPServer | None = None
persist_dir: pl.Path | None = None

ANSI_ESCAPE: Final = re.compile(r"(\x9B|\x1B\[)[0-?]*[ -/]*[@-~]")
ERROR_PREFIX: Final = re.compile(r"^[Ee][Rr][Rr]([Oo][Rr])?:\s*")

REQUEST_ADAPTER: Final = pyd.TypeAdapter[_types.Request](_types.Request)


def error_message(err: Any):
    return ERROR_PREFIX.sub("", ANSI_ESCAPE.sub("", str(err)))


def actual_remote_ip(request: tornado.httpserver.HTTPRequest):
    return str(request.remote_ip) if request.remote_ip else None


def forwarded_remote_ip(request: tornado.httpserver.HTTPRequest):
    return request.headers.get("X-Forwarded-For")


remote_ip: Callable[[tornado.httpserver.HTTPRequest], str | None] = actual_remote_ip


class IndexHandler(tornado.web.RequestHandler):
    def get(self):
        self.render("index.html")


@tornado.web.stream_request_body
class AddFile(tornado.web.RequestHandler):
    def prepare(self):
        self.fh: IO[bytes] | None = None
        self.metadata: mp3Juggler.FileTrackInput | None = None
        self.error: Exception | None = None
        self.done = False
        try:
            free = shutil.disk_usage(
                persist_dir if persist_dir is not None else tempfile.gettempdir()
            ).free
            if int(self.request.headers.get("Content-Length", 0)) > free / 2:
                raise Exception("Uploaded file too large for current free space")
            file_type = self.request.headers.get("Content-Type", "")
            if not file_type.startswith("audio/") and not file_type.startswith(
                "video/"
            ):
                raise Exception("Only audio or video files, please")
            filename = self.request.headers.get("Filename")
            if filename is not None:
                base, extn = os.path.splitext(filename)
            else:
                base, extn = None, None
            fd, path = tempfile.mkstemp(prefix=base, suffix=extn, dir=persist_dir)
            self.fh = os.fdopen(fd, "wb")
            self.metadata = mp3Juggler.FileTrackInput(
                upload_id=self.request.headers.get("Upload-Id"),
                nick=self.request.headers.get("Nick"),
                title=filename or "Unknown",
                filename=filename,
                extn=extn,
                address=remote_ip(self.request),
                mrl=path,
            )
        except Exception as err:
            self.error = err

    def data_received(self, chunk: bytes):
        if self.error is None:
            try:
                assert self.fh is not None
                self.fh.write(chunk)
            except Exception as err:
                self.error = err

    def put(self):
        try:
            if self.error is not None:
                raise self.error
            assert self.fh is not None
            self.fh.close()
            assert juggler is not None
            assert self.metadata is not None
            try:
                tags = tinytag.TinyTag.get(self.metadata.mrl)
                if tags.title:
                    title = tags.title
                    if tags.artist:
                        title = f"{tags.artist} - {title}"
                    self.metadata.title = title
                if tags.duration is not None:
                    self.metadata.duration = tags.duration
            except Exception as e:
                print(f"Error getting file metadata: {e}")
            juggler.juggle(self.metadata, self.request.headers.get("Parent-Id"))
            self.done = True
            self.finish()  # pyright: ignore[reportUnknownMemberType]
        except Exception as err:
            print(err)
            self.clear()
            self.set_status(500)
            self.finish(  # pyright: ignore[reportUnknownMemberType]
                error_message(err),
            )

    def on_finish(self):
        if not self.done:
            try:
                if self.fh is not None:
                    self.fh.close()
            except:
                pass
            self.done = True

    def on_connection_close(self):
        self.on_finish()
        super().on_connection_close()


class AddLink(tornado.web.RequestHandler):
    def post(self):
        try:
            link = self.request.body.decode()
            if (
                spotify_scraper
                and youtube_search
                and (
                    link.startswith("spotify:track:")
                    or link.startswith("https://open.spotify.com/track/")
                )
            ):
                with spotify_scraper.SpotifyClient() as client:
                    spotify_track = client.get_track(link)
                    results = cast(
                        list[dict[str, Any]],
                        youtube_search.YoutubeSearch(
                            f"{', '.join(artist.name for artist in spotify_track.artists)} - {spotify_track.name}"
                        ).to_dict(),
                    )
                    if results and (youtube_id := results[0].get("id")):
                        link = f"https://youtu.be/{youtube_id}"
                    else:
                        raise Exception(
                            "Failed to find alternative link for Spotify track, sorry!"
                        )
            if not link.startswith("http://") and not link.startswith("https://"):
                raise Exception("Only web links, please")
            with yt_dlp.YoutubeDL(
                {
                    "cookiefile": "cookies.txt",
                    "quiet": True,
                    "no_warnings": True,
                    "format": "bestaudio/best",
                }
            ) as ydl:
                info_dict = ydl.extract_info(link, download=False)
                title = info_dict.get("title") or link
                duration = info_dict.get("duration")
            assert juggler is not None
            juggler.juggle(
                mp3Juggler.LinkTrackInput(
                    upload_id=self.request.headers.get("Upload-Id"),
                    nick=self.request.headers.get("Nick"),
                    title=title,
                    duration=duration,
                    address=remote_ip(self.request),
                    mrl=link,
                ),
                self.request.headers.get("Parent-Id"),
            )
            self.finish()  # pyright: ignore[reportUnknownMemberType]
        except Exception as err:
            print(err)
            self.clear()
            self.set_status(500)
            self.finish(  # pyright: ignore[reportUnknownMemberType]
                error_message(err),
            )


class Download(tornado.web.RequestHandler):
    def get(self, track_id: str):
        try:
            assert juggler is not None
            info = juggler.download(track_id)
            if info is None:
                self.set_status(404)
                self.finish(  # pyright: ignore[reportUnknownMemberType]
                    "Not found",
                )
                return
            match info.type:
                case "file":
                    if info.filename is not None:
                        url_name = urllib.parse.quote(info.filename)
                        self.add_header(
                            "Content-Disposition",
                            'attachment; filename="' + url_name + '"',
                        )
                    with open(info.mrl, "rb") as f:
                        chunk = f.read(1048576)
                        while chunk:
                            self.write(  # pyright: ignore[reportUnknownMemberType]
                                chunk,
                            )
                            chunk = f.read(1048576)
                    self.finish()  # pyright: ignore[reportUnknownMemberType]
                case "link":
                    self.redirect(info.mrl)
        except Exception as err:
            print(err)
            self.clear()
            self.set_status(500)
            self.finish(  # pyright: ignore[reportUnknownMemberType]
                error_message(err),
            )


class WSHandler(tornado.websocket.WebSocketHandler):

    def open(self, *args: str, **kwargs: str):
        assert clients is not None
        assert juggler is not None
        clients.add_connection(self)
        self.write_message(
            _types.AddressResponse(address=remote_ip(self.request)).model_dump_json()
        )
        self.write_message(juggler.get_list().model_dump_json())

    def on_message(self, message: str | bytes):
        try:
            assert juggler is not None
            request = REQUEST_ADAPTER.validate_json(message)
            match request.type:
                case "cancel":
                    juggler.cancel(request.id, remote_ip(self.request))
        except Exception as err:
            print(err)
            self.write_message(
                json.dumps({"type": "error", "message": error_message(err)})
            )

    def on_close(self):
        assert clients is not None
        print("connection closed")
        clients.close_connection(self)


def start(config: _config.PrinterConfig):
    global loop, clients, juggler, http_server, persist_dir, remote_ip

    if config.proxied:
        remote_ip = forwarded_remote_ip
    persist_dir = config.persist_dir

    loop = tornado.ioloop.IOLoop.current()

    clients = connections.Connections(loop)
    juggler = mp3Juggler.Juggler(clients, config)

    application = tornado.web.Application(
        [
            (r"/ws", WSHandler),
            (r"/", IndexHandler),
            (r"/add-file", AddFile),
            (r"/add-link", AddLink),
            (r"/download/(.*)", Download),
        ],
        # compiled_template_cache=False,  # Useful when editing index.html
        static_path=os.path.join(os.path.dirname(__file__), "static"),
    )

    http_server = tornado.httpserver.HTTPServer(
        application,
        max_body_size=1024 * 1024 * 1024,  # 1GiB
    )
    assert http_server is not None
    http_server.listen(port=config.port, address=config.bind)
    print("*** Web Server Started on %s:%s***" % (config.bind or "*", config.port))

    threading.Thread(target=loop.start).start()
    juggler.start()


def stop():
    if loop is not None:
        # Should use add_callback_from_signal according to documentation, but it's deprecated
        # on master (since 2023-05-02), and add_callback should have the same effect since 6.0.
        loop.add_callback(  # pyright: ignore[reportUnknownMemberType]
            lambda: loop.stop() if loop is not None else None
        )
    if http_server is not None:
        http_server.stop()
    if juggler is not None:
        juggler.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Musical democracy")
    if pychromecast:
        parser.add_argument(
            "-C",
            "--list-chromecasts",
            action="store_true",
            help="List available Chromecast (and Chromecast group) names and exit",
        )
    parser.add_argument(
        "config_file",
        metavar="CONFIG_FILE",
        type=str,
        nargs="?",
        help="Config file to use (default 'config.toml')",
        default="config.toml",
    )
    args = parser.parse_args()

    if pychromecast:
        if args.list_chromecasts:
            print("Scanning for Chromecasts...")
            services, browser = pychromecast.discovery.discover_chromecasts()
            pychromecast.discovery.stop_discovery(browser)
            if services:
                print("Available Chromecast targets:")
                for service in services:
                    print('* "%s"' % service.friendly_name)
            else:
                print("No Chromecast targets found.")
            exit(0)

    def signal_handler(sig: int, _: Any):
        print(f"\nSignal {sig} caught, exiting...")
        stop()
        exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    config = _config.parse(args.config_file)

    try:
        start(config)
    except Exception as err:
        print("Error starting web server:", err)
        exit(1)

    # Start console
    while True:
        match input():
            case "s" | "skip":
                if juggler is not None:
                    print("Skipping...")
                    juggler.skip()
            case "c" | "clear":
                if juggler is not None:
                    print("Clearing...")
                    juggler.clear()
            case "p" | "play" | "pause":
                if juggler is not None:
                    print("Toggling pause...")
                    juggler.pause()
            case other:
                print(f"Unknown command '{other}'")
