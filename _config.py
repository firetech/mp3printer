# pyright: strict

import os
import pathlib as pl
import tomllib as tl
from typing import Annotated, Any, Iterable, cast

import pydantic as pyd

# optional libs
try:
    # Chromecast support
    import pychromecast.discovery
except ModuleNotFoundError:
    pychromecast = None


def _normalize_source(inp: Any) -> tuple[str, ...]:
    if isinstance(inp, str):
        return (inp,)
    if isinstance(inp, Iterable):
        return tuple(str(item) for item in cast(Iterable[Any], inp))
    raise ValueError("source must be str or iterable of str")


class FallbackConfig(pyd.BaseModel):
    name: str
    source: Annotated[tuple[str, ...], pyd.BeforeValidator(_normalize_source)]
    random: bool = False

    model_config = {"extra": "forbid"}


def _normalize_persist_dir(inp: Any) -> pl.Path | None:
    if inp:
        path = pl.Path(inp)
        os.makedirs(path, exist_ok=True)
        return path
    return None


def _handle_chromecast(inp: Any) -> tuple[str, int] | None:
    if inp:
        if not isinstance(inp, str):
            raise ValueError(f"Expected str, got {type(inp).__name__}")
        if pychromecast:
            services, browser = pychromecast.discovery.discover_listed_chromecasts(
                friendly_names=[inp]
            )
            pychromecast.discovery.stop_discovery(browser)
            if len(services) < 1:
                raise Exception(f'Could not find Chromecast (or group) "{inp}"')
            elif len(services) > 1:
                raise Exception(f'More than one Chromecast (or group) matched "{inp}"')

            return (services[0].host, services[0].port)
        else:
            raise Exception("chromecast is set, but pychromecast is not installed.")
    return None


def _normalize_fallback(inp: Any) -> tuple[Any, ...]:
    if isinstance(inp, Iterable):
        return tuple(cast(Iterable[Any], inp))
    raise ValueError("fallback must be iterable")


class PrinterConfig(pyd.BaseModel):
    bind: str | None = None
    port: int = 80
    proxied: bool = False

    prio_dropoff: int = 3
    persist_dir: Annotated[
        pl.Path | None, pyd.BeforeValidator(_normalize_persist_dir)
    ] = None

    chromecast: Annotated[
        tuple[str, int] | None, pyd.BeforeValidator(_handle_chromecast)
    ] = None

    fallback: Annotated[
        tuple[FallbackConfig, ...], pyd.BeforeValidator(_normalize_fallback)
    ] = ()

    model_config = {"extra": "forbid"}


def parse(config_file: str | os.PathLike[str]) -> PrinterConfig:
    with open(config_file, "rb") as f:
        return PrinterConfig.model_validate(tl.load(f))
