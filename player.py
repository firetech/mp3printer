# pyright: strict

import enum
import random
from typing import (
    Any,
    Final,
    Literal,
    NotRequired,
    Protocol,
    TypeAlias,
    TypedDict,
    cast,
)

import pydantic as pyd
import vlc  # pyright: ignore[reportMissingTypeStubs]
import yt_dlp


class PlayerListener(Protocol):
    def song_finished(self) -> None: ...


class CommonTrackInfo(pyd.BaseModel):
    title: str
    mrl: str


class FileTrackInfo(CommonTrackInfo):
    type: Literal["file"] = "file"


class LinkTrackInfo(CommonTrackInfo):
    type: Literal["link"] = "link"


TrackInfo: TypeAlias = FileTrackInfo | LinkTrackInfo


class FallbackType(enum.Enum):
    NONE = None
    SLAYRADIO = "slayradio"
    DUBSTEP = "dubstep"
    POPLOVE = "poplove"


class PlayerArgs(TypedDict):
    chromecast: NotRequired[tuple[str, int]]
    disabled_fallbacks: NotRequired[set[FallbackType]]


class Player:

    SLAYRADIO: Final = "http://relay.slayradio.org:8000/"
    DUBSTEP: Final = {
        "https://www.youtube.com/watch?v=dLyH94jNau0",
        "https://www.youtube.com/watch?v=RRucF7ffPRE",
        "https://www.youtube.com/watch?v=nXaMKZApYDM",
    }
    POPLOVE: Final = {
        "https://www.youtube.com/watch?v=QcYn1xM7VZg",
        "https://www.youtube.com/watch?v=b6MGVFBpi9Q",
        "https://www.youtube.com/watch?v=KuPBK8Bx3iI",
        "https://www.youtube.com/watch?v=mX8f5nfkr7E",
        "https://www.youtube.com/watch?v=TXGtR1_iyDg",
        "https://www.youtube.com/watch?v=KPFVrhUdrHk",
        "https://www.youtube.com/watch?v=0YVwtb8pZZY",
        "https://www.youtube.com/watch?v=XUkU0noAGck",
        "https://www.youtube.com/watch?v=BpEQMV_Pih0",
        "https://www.youtube.com/watch?v=zQAFN1NZd50",
        "https://www.youtube.com/watch?v=-sAEJaKwFZM",
    }
    SCRATCH: Final = "shortscratch.wav"

    def __init__(self, listener: PlayerListener, args: PlayerArgs):
        self._listener = listener
        instance_opts = ["--no-video"]
        self._media_opts: list[str] = []
        if chromecast := args.get("chromecast"):
            instance_opts.append("--no-sout-video")
            # These options don't work as instance options, for some reason...
            self._media_opts.append(":sout=#chromecast{ip=%s,port=%d}" % chromecast)
            self._media_opts.append(":demux-filter=demux_chromecast")
        self._disabled_fallbacks: set[FallbackType] = set()
        if disabled_fallbacks := args.get("disabled_fallbacks"):
            # Don't allow disabling NONE.
            self._disabled_fallbacks.update(disabled_fallbacks - {FallbackType.NONE})
        self._instance = cast(vlc.Instance, vlc.Instance(*instance_opts))
        self._mediaplayer = cast(
            vlc.MediaPlayer,
            self._instance.media_player_new(),  # pyright: ignore[reportUnknownMemberType]
        )
        vlc_events = cast(
            vlc.EventManager,
            self._mediaplayer.event_manager(),  # pyright: ignore[reportUnknownMemberType]
        )

        def _event_wrapper(*args: Any):
            listener.song_finished()

        vlc_events.event_attach(  # pyright: ignore[reportUnknownMemberType]
            vlc.EventType.MediaPlayerEndReached,  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownArgumentType]
            _event_wrapper,
            1,
        )
        vlc_events.event_attach(  # pyright: ignore[reportUnknownMemberType]
            vlc.EventType.MediaPlayerEncounteredError,  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownArgumentType]
            _event_wrapper,
            1,
        )

        self._fallback_type = FallbackType.NONE
        self._previous_fallback_type = FallbackType.NONE
        self._current_fallback_track = None

    def release(self):
        self._mediaplayer.stop()
        self._instance.release()

    def _clearFallback(self):
        self._fallback_type = FallbackType.NONE
        self._current_fallback_track = None

    def _get_link_url(self, link: str):
        with yt_dlp.YoutubeDL(
            {
                "cookiefile": "cookies.txt",
                "quiet": True,
                "format": "bestaudio/best",
            }
        ) as ydl:
            info_dict = ydl.extract_info(link, download=False)
            return info_dict.get("url", None)

    def _play_mrl(self, mrl: str, position: float | None = None):
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
            self._clearFallback()
            print("Now playing: " + track.title)
            mrl = track.mrl
            if (
                track.type == "link"
                and (link_mrl := self._get_link_url(mrl)) is not None
            ):
                mrl = link_mrl
            self._play_mrl(mrl, position)
        except Exception as err:
            print(err)
            self._listener.song_finished()

    def pause(self):
        self._mediaplayer.pause()

    def scratch(self):
        self._clearFallback()
        self._play_mrl(self.SCRATCH)

    def get_position(self):
        return cast(float, self._mediaplayer.get_position())

    @property
    def fallback_type(self):
        match self._fallback_type:
            case FallbackType.NONE:
                return "nothing"
            case FallbackType.SLAYRADIO:
                return "Slay Radio"
            case FallbackType.DUBSTEP:
                return "dubstep"
            case FallbackType.POPLOVE:
                return "PopLove"

    def play_fallback(self):
        try:
            last_fallback_type = self._fallback_type
            if self._fallback_type == FallbackType.NONE:
                available_types = (
                    set(FallbackType) - {FallbackType.NONE} - self._disabled_fallbacks
                )
                if not available_types:
                    available_types.add(FallbackType.NONE)
                if len(available_types) == 1:
                    self._fallback_type = available_types.pop()
                else:
                    # Make sure we don't select previous type again
                    self._fallback_type = random.choice(
                        tuple(available_types - {self._previous_fallback_type})
                    )
                self._previous_fallback_type = self._fallback_type
            match self._fallback_type:
                case FallbackType.NONE:
                    pass  # Do nothing
                case FallbackType.SLAYRADIO:
                    print("Now playing: Slay Radio")
                    self._play_mrl(self.SLAYRADIO)
                case FallbackType.DUBSTEP:
                    print("Now playing: Dubstep")
                    self._play_fallback_list(
                        self.DUBSTEP, last_fallback_type == FallbackType.NONE
                    )
                case FallbackType.POPLOVE:
                    print("Now playing: PopLove")
                    self._play_fallback_list(
                        self.POPLOVE, last_fallback_type == FallbackType.NONE
                    )
        except Exception as err:
            print(err)
            self._fallback_type = FallbackType.NONE
            self._listener.song_finished()

    def _play_fallback_list(self, tracks: set[str], start_random: bool):
        # Make sure we select a new track (unless list is a single item)
        track_choices = tracks - {self._current_fallback_track}
        if track_choices:
            self._current_fallback_track = random.choice(tuple(track_choices))
        url = self._get_link_url(self._current_fallback_track)
        assert url is not None, "Error getting fallback URL"
        self._play_mrl(url, random.random() if start_random else None)
