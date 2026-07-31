# pyright: strict

import collections
import os
import pathlib as pl
import tempfile
import threading
import time
import uuid
from typing import Final, Literal, TypeAlias

import pydantic as pyd

# local libs
import _types
import connections
import player

STATE_FILENAME: Final = "state.json"


class TrackInputCommon(pyd.BaseModel):
    upload_id: str | None = None
    nick: str | None = None
    address: str | None
    title: str
    duration: float | None = None
    mrl: str


class FileTrackInput(TrackInputCommon):
    type: Literal["file"] = "file"
    filename: str | None = None
    extn: str | None = None


class LinkTrackInput(TrackInputCommon):
    type: Literal["link"] = "link"


class TrackAssigned(pyd.BaseModel):
    id: str
    prio: int


class FileTrack(TrackAssigned, FileTrackInput, player.FileTrackInfo): ...


class LinkTrack(TrackAssigned, LinkTrackInput, player.LinkTrackInfo): ...


TrackInput: TypeAlias = FileTrackInput | LinkTrackInput
Track: TypeAlias = FileTrack | LinkTrack


class PersistentState(pyd.BaseModel):
    queue: list[Track]
    position: float | None


class DownloadInfo(pyd.BaseModel):
    type: Literal["file", "link"]
    filename: str | None
    mrl: str


class _ParentWaiter:
    def __init__(self, lock: threading.RLock) -> None:
        self._cond = threading.Condition(lock)
        self._success = False

    def wait(self, timeout: float | None = None):
        if self._cond.wait(timeout):
            return self._success
        return False

    def done(self, success: bool):
        self._success = success
        self._cond.notify_all()


class Juggler(player.PlayerListener):
    def __init__(
        self,
        clients: connections.Connections,
        persist_dir: os.PathLike[str] | str | None = None,
        player_args: player.PlayerArgs | None = None,
    ):
        self._clients = clients
        if persist_dir:
            self._persist_dir = pl.Path(persist_dir).resolve()
            self._expected_file_dir = self._persist_dir
        else:
            self._persist_dir = None
            self._expected_file_dir = pl.Path(tempfile.gettempdir()).resolve()
        self._start_position = None
        self._player_args: player.PlayerArgs = player_args or {}
        self._songlist: list[Track] = []
        self._counts: collections.Counter[str | None] = collections.Counter()
        self._event = threading.Event()
        self._waiting: dict[str, _ParentWaiter] = {}
        self._running = False
        self.lock = threading.RLock()
        self._load_persist_state()

    def __del__(self):
        self.stop()

    def _load_persist_state(self):
        if self._persist_dir is None:
            return
        state_file = self._persist_dir / STATE_FILENAME
        if not state_file.is_file():
            return
        try:
            with state_file.open("r") as f:
                state = PersistentState.model_validate_json(f.read())
            self.clear()
            self._songlist = state.queue
            self._counts = collections.Counter(
                [song.address for song in self._songlist]
            )
            self._start_position = state.position
        except Exception as e:
            print(f"Error loading persistent state: {e}")

    def _remove_song_file(self, song: TrackInput):
        if isinstance(song, FileTrackInput):
            try:
                mrl_path = pl.Path(song.mrl).resolve()
                if mrl_path.is_file() and mrl_path.is_relative_to(
                    self._expected_file_dir
                ):
                    mrl_path.unlink()
            except:
                pass

    def _remove_song(self, index: int, song: Track | None = None):
        if song is None:
            song = self._songlist[index]
        self._remove_song_file(song)
        self._counts[song.address] -= 1
        del self._songlist[index]

    def start(self):
        if not self._running:
            self._player = player.Player(self, self._player_args)
            self._next_thread = threading.Thread(target=self.play_next, args=())
            self._progress_thread = threading.Thread(target=self.time_change, args=())
            self._running = True
            self._next_thread.start()
            self._progress_thread.start()
            self._clients.message_clients(self.get_list())
            if self._songlist:
                self._player.play(self._songlist[0], self._start_position)
            else:
                self._player.play_fallback()
            self._start_position = None

    def stop(self):
        if self._running:
            self._running = False
            self._event.set()
            if self._persist_dir is None:
                self.clear()
            else:
                self._start_position = (
                    self._player.get_position() if self._songlist else None
                )
                state = PersistentState(
                    queue=self._songlist,
                    position=self._start_position,
                )
                with (self._persist_dir / STATE_FILENAME).open("w") as f:
                    f.write(state.model_dump_json(indent=2))
            self._next_thread.join()
            self._progress_thread.join()
            self._player.release()

    def skip(self):
        self.lock.acquire()
        try:
            self._player.scratch()
        finally:
            self.lock.release()

    def pause(self):
        self.lock.acquire()
        try:
            self._player.pause()
        finally:
            self.lock.release()

    def juggle(self, infile: TrackInput, parent_id: str | None = None):
        if not self._running:
            raise Exception("Queue is not running")

        threading.Thread(target=self._juggle, args=(infile, parent_id)).start()

    def _juggle(self, track_input: TrackInput, parent_id: str | None = None):
        self.lock.acquire()
        try:
            if parent_id is not None:
                for song in reversed(self._songlist):
                    if song.upload_id == parent_id:
                        break
                else:  # Not found
                    remove = True
                    try:
                        if not parent_id in self._waiting:
                            self._waiting[parent_id] = _ParentWaiter(self.lock)
                        if self._waiting[parent_id].wait(30):
                            remove = False
                        else:
                            return
                    finally:
                        if remove:
                            self._remove_song_file(track_input)

            self._counts[track_input.address] += 1
            prio = max(self._counts[track_input.address] - 3, 0)
            index = 0
            if len(self._songlist) > 0:
                index = 1
                for item in self._songlist[1:]:
                    if item.prio > prio:
                        break
                    index += 1
            extn = track_input.extn if isinstance(track_input, FileTrackInput) else None
            queue_id = str(uuid.uuid4()) + (extn or "")
            match track_input.type:
                case "file":
                    track = FileTrack(
                        id=queue_id,
                        prio=prio,
                        upload_id=track_input.upload_id,
                        nick=track_input.nick,
                        title=track_input.title,
                        duration=track_input.duration,
                        filename=track_input.filename,
                        extn=track_input.extn,
                        address=track_input.address,
                        mrl=track_input.mrl,
                    )
                case "link":
                    track = LinkTrack(
                        id=queue_id,
                        prio=prio,
                        upload_id=track_input.upload_id,
                        nick=track_input.nick,
                        title=track_input.title,
                        duration=track_input.duration,
                        address=track_input.address,
                        mrl=track_input.mrl,
                    )
            self._songlist.insert(index, track)

            if len(self._songlist) == 1:
                self._player.play(track)

            if (
                track_input.upload_id is not None
                and track_input.upload_id in self._waiting
            ):
                self._waiting[track_input.upload_id].done(True)
                del self._waiting[track_input.upload_id]
        finally:
            self.lock.release()
        self._clients.message_clients(self.get_list())

    def download(self, track_id: str) -> DownloadInfo | None:
        self.lock.acquire()
        try:
            for song in self._songlist:
                if song.id == track_id:
                    return DownloadInfo(
                        type=song.type,
                        filename=(
                            song.filename if isinstance(song, FileTrack) else None
                        ),
                        mrl=song.mrl,
                    )
            else:  # Not found
                return None
        finally:
            self.lock.release()

    def cancel(self, track_id: str, address: str | None):
        self.lock.acquire()
        try:
            for i, song in list(enumerate(self._songlist)):
                if song.id == track_id and song.address == address:
                    if i == 0:
                        self.skip()
                    else:
                        self._remove_song(i, song)
                    break
        finally:
            self.lock.release()
        self._clients.message_clients(self.get_list())

    def clear(self):
        self.lock.acquire()
        try:
            for wait in self._waiting.values():
                wait.done(False)
            self._waiting.clear()
            for i, song in reversed(list(enumerate(self._songlist))):
                if i == 0:
                    self.skip()
                self._remove_song(i, song)
        finally:
            self.lock.release()
        self._clients.message_clients(self.get_list())

    def song_finished(self):
        self._event.set()

    def time_change(self):
        while self._running:
            time.sleep(1)
            self.send_progress()

    def send_progress(self):
        self.lock.acquire()
        try:
            position = self._player.get_position()
        finally:
            self.lock.release()
        if position > 0:
            self._clients.message_clients(_types.ProgressResponse(position=position))

    def play_next(self):
        while self._running:
            self._event.wait()
            self._event.clear()
            if not self._running:
                break
            self.lock.acquire()
            try:
                if not self._songlist:
                    self._player.play_fallback()
                else:
                    self._remove_song(0)
                    if not self._songlist:
                        self._player.play_fallback()
                    else:
                        track = self._songlist[0]
                        self._player.play(track)
            finally:
                self.lock.release()
            self._clients.message_clients(self.get_list())

    def get_list(self) -> _types.ListResponse:
        self.lock.acquire()
        try:
            if self._songlist:
                return _types.ListContentResponse(
                    position=self._player.get_position(),
                    list=[
                        _types.ListContentResponseEntry(
                            id=item.id,
                            title=item.title,
                            duration=item.duration,
                            nick=item.nick,
                            address=item.address,
                            prio=item.prio,
                        )
                        for item in self._songlist
                    ],
                )
            else:
                if self._running:
                    if (fallback_type := self._player.fallback_type) is not None:
                        message = f"Now playing {fallback_type}..."
                    else:
                        message = "Currently silent. Add something to the queue!"
                else:
                    message = "Not active"
                return _types.ListFallbackResponse(description=message)
        finally:
            self.lock.release()
