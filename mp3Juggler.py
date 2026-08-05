# pyright: strict

from __future__ import annotations

import collections
import pathlib as pl
import tempfile
import threading
import time
import uuid
from typing import Annotated, Final, Literal, TypeAlias

import pydantic as pyd

# local libs
import _config
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


class _TrackAssigned(pyd.BaseModel):
    id: str
    prio: int
    next: Annotated[Track | None, pyd.Field(exclude=True)] = None


class FileTrack(_TrackAssigned, FileTrackInput, player.FileTrackInfo): ...


class LinkTrack(_TrackAssigned, LinkTrackInput, player.LinkTrackInfo): ...


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
        config: _config.PrinterConfig,
    ):
        self._clients = clients
        self._config = config
        if config.persist_dir:
            self._persist_dir = config.persist_dir.resolve()
            self._expected_file_dir = self._persist_dir
        else:
            self._persist_dir = None
            self._expected_file_dir = pl.Path(tempfile.gettempdir()).resolve()
        self._player: player.Player | None = None
        self._start_position = None
        self._songlist: Track | None = None
        self._upload_ids: collections.Counter[str | None] = collections.Counter()
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
        self.lock.acquire()
        try:
            with state_file.open("r") as f:
                state = PersistentState.model_validate_json(f.read())
            self.clear()
            prev: Track | None = None
            for track in state.queue:
                if prev is None:
                    self._songlist = track
                else:
                    prev.next = track
                prev = track
            self._counts = collections.Counter([song.address for song in state.queue])
            self._upload_ids = collections.Counter(
                [song.upload_id for song in state.queue]
            )
            self._start_position = state.position
        except Exception as e:
            print(f"Error loading persistent state: {e}")
        finally:
            self.lock.release()

    def _songlist_items(self):
        song = self._songlist
        while song is not None:
            yield song
            song = song.next

    def _store_persist_state(self, set_start_pos: bool = False):
        self.lock.acquire()
        try:
            if self._persist_dir is None:
                return
            position = (
                self._player.get_position()
                if self._player is not None and self._songlist is not None
                else None
            )
            if set_start_pos:
                self._start_position = position
            state = PersistentState(
                queue=list(self._songlist_items()),
                position=position,
            )
            with (self._persist_dir / STATE_FILENAME).open("w") as f:
                f.write(state.model_dump_json(indent=2))
        finally:
            self.lock.release()

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

    def _remove_next(self, prev: Track | None = None):
        self.lock.acquire()
        try:
            if prev is None:
                song = self._songlist
            else:
                song = prev.next
            if song is None:
                return
            self._remove_song_file(song)
            self._counts[song.address] -= 1
            self._upload_ids[song.upload_id] -= 1
            if prev is None:
                self._songlist = song.next
            else:
                prev.next = song.next
        finally:
            self.lock.release()

    def start(self):
        if not self._running:
            self._player = player.Player(self, self._config)
            self._next_thread = threading.Thread(target=self.play_next, args=())
            self._progress_thread = threading.Thread(target=self.time_change, args=())
            self._running = True
            self._next_thread.start()
            self._progress_thread.start()
            self._clients.message_clients(self.get_list())
            if self._songlist:
                self._player.play(self._songlist, self._start_position)
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
                self._store_persist_state(True)
            self._next_thread.join()
            self._progress_thread.join()
            if self._player is not None:
                self._player.release()
            self._player = None

    def skip(self):
        self.lock.acquire()
        try:
            if self._player is not None:
                self._player.scratch()
        finally:
            self.lock.release()

    def pause(self):
        self.lock.acquire()
        try:
            if self._player is not None:
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
                if not self._upload_ids[parent_id]:
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
            if self._config.prio_dropoff <= 0:
                prio = 1
            else:
                prio = (
                    max(
                        (self._counts[track_input.address] + 1)
                        - self._config.prio_dropoff,
                        0,
                    )
                    + 1
                )
            prev = self._songlist
            if prev is not None:
                while prev.next is not None and prev.next.prio <= prio:
                    prev = prev.next

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
            if prev is None:
                track.next = self._songlist
                self._songlist = track
            else:
                track.next = prev.next
                prev.next = track
            self._counts[track.address] += 1
            self._upload_ids[track.upload_id] += 1

            self._store_persist_state()

            if prev is None and self._player is not None:
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
            for song in self._songlist_items():
                if song.id == track_id:
                    return DownloadInfo(
                        type=song.type,
                        filename=(
                            song.filename if isinstance(song, FileTrack) else None
                        ),
                        mrl=song.mrl,
                    )
            return None
        finally:
            self.lock.release()

    def cancel(self, track_id: str, address: str | None):
        self.lock.acquire()
        try:
            prev = None
            for song in self._songlist_items():
                if song.id == track_id and song.address == address:
                    if prev is None:
                        self.skip()
                    else:
                        self._remove_next(prev)
                    break
                prev = song
            self._store_persist_state()
        finally:
            self.lock.release()
        self._clients.message_clients(self.get_list())

    def clear(self):
        self.lock.acquire()
        try:
            for wait in self._waiting.values():
                wait.done(False)
            self._waiting.clear()
            had_content = self._songlist is not None
            while self._songlist is not None:
                self._remove_next()
            if self._running and had_content:
                self.skip()
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
            if self._player is not None:
                position = self._player.get_position()
            else:
                position = 0.0
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
            assert self._player is not None
            try:
                if self._songlist is None:
                    self._player.play_fallback()
                else:
                    self._remove_next()
                    if not self._songlist:
                        self._player.play_fallback()
                    else:
                        self._player.play(self._songlist)
            finally:
                self.lock.release()
            self._clients.message_clients(self.get_list())
            self._store_persist_state()

    def get_list(self) -> _types.ListResponse:
        self.lock.acquire()
        try:
            if self._songlist is not None:
                return _types.ListContentResponse(
                    position=(
                        self._player.get_position() if self._player is not None else 0.0
                    ),
                    list=[
                        _types.ListContentResponseEntry(
                            id=item.id,
                            title=item.title,
                            duration=item.duration,
                            nick=item.nick,
                            address=item.address,
                            prio=item.prio,
                        )
                        for item in self._songlist_items()
                    ],
                )
            else:
                if self._running:
                    if (
                        self._player is not None
                        and (fallback_type := self._player.fallback_name) is not None
                    ):
                        message = f"Now playing {fallback_type}..."
                    else:
                        message = "Currently silent. Add something to the queue!"
                else:
                    message = "Not active"
                return _types.ListFallbackResponse(description=message)
        finally:
            self.lock.release()
