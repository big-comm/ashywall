"""The online providers AshyWall knows, in sidebar order."""

from .base import RemoteProvider
from .bing import BingProvider
from .commons import CommonsProvider
from .wallhaven import WallhavenProvider

PROVIDER_NAMES: dict[str, str] = {
    WallhavenProvider.id: WallhavenProvider.display_name,
    BingProvider.id: BingProvider.display_name,
    CommonsProvider.id: CommonsProvider.display_name,
}


def create_providers() -> dict[str, RemoteProvider]:
    return {
        provider.id: provider
        for provider in (WallhavenProvider(), BingProvider(), CommonsProvider())
    }


def provider_name(origin: str) -> str:
    return PROVIDER_NAMES.get(origin, origin)
