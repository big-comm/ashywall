"""The online providers AshyWall knows, in sidebar order."""

from pathlib import Path

from .base import RemoteProvider
from .biglinux import BigLinuxProvider
from .bing import BingProvider
from .commons import CommonsProvider
from .wallhaven import WallhavenProvider

PROVIDER_NAMES: dict[str, str] = {
    BigLinuxProvider.id: BigLinuxProvider.display_name,
    WallhavenProvider.id: WallhavenProvider.display_name,
    BingProvider.id: BingProvider.display_name,
    CommonsProvider.id: CommonsProvider.display_name,
}


def create_providers(cache_dir: Path | None = None) -> dict[str, RemoteProvider]:
    biglinux_cache = cache_dir / "biglinux.json" if cache_dir is not None else None
    return {
        provider.id: provider
        for provider in (
            BigLinuxProvider(biglinux_cache),
            WallhavenProvider(),
            BingProvider(),
            CommonsProvider(),
        )
    }


def provider_name(origin: str) -> str:
    return PROVIDER_NAMES.get(origin, origin)
