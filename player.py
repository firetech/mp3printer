# pyright: strict

import glob
import os
import pathlib as pl
import random
from typing import Any, Final, Iterable, Literal, Protocol, TypeAlias, cast

import pydantic as pyd
import vlc  # pyright: ignore[reportMissingTypeStubs]
import yt_dlp

import _config


class PlayerListener(Protocol):
    def track_finished(self) -> None: ...


class CommonTrackInfo(pyd.BaseModel):
    title: str
    mrl: str


class FileTrackInfo(CommonTrackInfo):
    type: Literal["file"] = "file"


class LinkTrackInfo(CommonTrackInfo):
    type: Literal["link"] = "link"


TrackInfo: TypeAlias = FileTrackInfo | LinkTrackInfo

SCRATCH: Final = pl.Path("shortscratch.wav").resolve()


class Player:

    def __init__(self, listener: PlayerListener, config: _config.PrinterConfig):
        self._listener = listener
        instance_opts = ["--no-video"]
        self._media_opts: list[str] = []
        if config.chromecast:
            instance_opts.append("--no-sout-video")
            # These options don't work as instance options, for some reason...
            self._media_opts.append(
                ":sout=#chromecast{ip=%s,port=%d}" % config.chromecast
            )
            self._media_opts.append(":demux-filter=demux_chromecast")
        self._fallbacks = config.fallback
        self._instance = cast(vlc.Instance, vlc.Instance(*instance_opts))
        self._mediaplayer = cast(
            vlc.MediaPlayer,
            self._instance.media_player_new(),  # pyright: ignore[reportUnknownMemberType]
        )
        vlc_events = cast(
            vlc.EventManager,
            self._mediaplayer.event_manager(),  # pyright: ignore[reportUnknownMemberType]
        )

        vlc_events.event_attach(  # pyright: ignore[reportUnknownMemberType]
            vlc.EventType.MediaPlayerEndReached,  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownArgumentType]
            self._vlc_end_reached,
            1,
        )
        vlc_events.event_attach(  # pyright: ignore[reportUnknownMemberType]
            vlc.EventType.MediaPlayerEncounteredError,  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownArgumentType]
            self._vlc_encountered_error,
            1,
        )

        self._fallback_idx: int = -1
        self._bad_fallbacks: set[int] = set()
        self._previous_fallback_idx: int = -1
        self._fallback_source_cache: tuple[str | pl.Path, ...] | None = None
        self._fallback_source_idx: int | None = None

    def _mark_fallback_bad(self):
        if self._fallback_idx >= 0:
            self._bad_fallbacks.add(self._fallback_idx)
            self._fallback_idx = -1

    def _vlc_end_reached(self, *args: Any):
        if self._mediaplayer.get_length() == 0:
            # Length is 0 for unplayable media, but also for web radio
            # However, if a web radio stream gets "end reached", it's probably an error
            self._mark_fallback_bad()

        self._listener.track_finished()

    def _vlc_encountered_error(self, *args: Any):
        self._mark_fallback_bad()
        self._listener.track_finished()

    def release(self):
        self._mediaplayer.stop()
        self._instance.release()

    def _clear_fallback(self, clear_cache: bool = True):
        self._fallback_idx = -1
        self._current_fallback_src_idx = None
        if clear_cache:
            self._bad_fallbacks.clear()
            self._fallback_source_cache = None

    def _get_link_url(self, link: str):
        try:
            with yt_dlp.YoutubeDL(
                {
                    "cookiefile": "cookies.txt",
                    "quiet": True,
                    "no_warnings": True,
                    "format": "bestaudio/best",
                }
            ) as ydl:
                info_dict = ydl.extract_info(link, download=False)
                if (link_url := info_dict.get("url")) is not None:
                    return link_url
        except:
            # yt_dlp prints its own error
            pass
        return link

    def _play_mrl(self, mrl: str | os.PathLike[str], position: float | None = None):
        self._mediaplayer.set_mrl(  # pyright: ignore[reportUnknownMemberType]
            mrl, *self._media_opts
        )
        self._mediaplayer.play()
        if position is not None:
            self._mediaplayer.set_position(  # pyright: ignore[reportUnknownMemberType]
                position
            )

    def play(self, track: TrackInfo, position: float | None = None):
        try:
            self._clear_fallback()
            print("Now playing: " + track.title)
            mrl = track.mrl
            if track.type == "link":
                mrl = self._get_link_url(mrl)
            self._play_mrl(mrl, position)
        except Exception as err:
            print(err)
            self._listener.track_finished()

    def pause(self):
        self._mediaplayer.pause()

    def scratch(self):
        self._clear_fallback()
        self._play_mrl(SCRATCH)

    def get_position(self):
        return cast(float, self._mediaplayer.get_position())

    @property
    def fallback_name(self):
        if self._fallback_idx < 0:
            return None
        return self._fallbacks[self._fallback_idx].name

    def play_fallback(self):
        try:
            old_fallback_idx = self._fallback_idx
            if self._fallback_idx < 0:
                fallback_choices = (
                    set(range(len(self._fallbacks))) - self._bad_fallbacks
                )
                if len(fallback_choices) == 1:
                    self._fallback_idx = fallback_choices.pop()
                elif fallback_choices:
                    # Make sure we don't select previous type again
                    self._fallback_idx = random.choice(
                        tuple(fallback_choices - {self._previous_fallback_idx})
                    )
                if self._previous_fallback_idx != self._fallback_idx:
                    self._fallback_source_cache = None
                self._previous_fallback_idx = self._fallback_idx
            if self._fallback_idx < 0:
                print("No fallback to play")
                return
            fallback = self._fallbacks[self._fallback_idx]
            if not self._fallback_source_cache:
                # Traverse directories as late as possible, so new files are found.
                def _fallback_url_or_path(s: str) -> Iterable[str | pl.Path]:
                    if s.startswith("FILE:"):
                        path = os.path.expanduser(s[5:])
                        return (pl.Path(f) for f in glob.glob(path, recursive=True))
                    return (s,)

                self._fallback_source_cache = tuple(
                    out_item
                    for inp_item in fallback.source
                    for out_item in _fallback_url_or_path(str(inp_item))
                )
            sources = self._fallback_source_cache
            if not sources:
                self._fallback_source_cache = None
                raise Exception(f"No sources in fallback '{fallback.name}'")
            if fallback.random:
                # Make sure we select a new track (unless list is a single item)
                source_choices = set(range(len(sources))) - {self._fallback_source_idx}
                if source_choices:
                    self._fallback_source_idx = random.choice(tuple(source_choices))
                position = random.random() if old_fallback_idx < 0 else None
            else:
                # Play list in order
                self._fallback_source_idx = (
                    0
                    if self._fallback_source_idx is None
                    else (self._fallback_source_idx + 1) % len(sources)
                )
                position = None
            assert self._fallback_source_idx is not None
            orig_mrl = mrl = sources[self._fallback_source_idx]
            if isinstance(mrl, str):
                mrl = self._get_link_url(mrl)
            print(f"Now playing: {fallback.name} ({orig_mrl})")
            self._play_mrl(mrl, position)
        except Exception as err:
            print(f"Error playing fallback: {err}")
            self._bad_fallbacks.add(self._fallback_idx)
            self._clear_fallback(False)
            self._listener.track_finished()
