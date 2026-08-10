"""src/assets — multi-asset abstraction layer.

The public surface for the engine is intentionally small. Engine code
should only need:

    from src.assets import resolve, AssetSpec
    from src.assets.types import Quantity, Money, Price, Currency
    from src.assets.types import shares, contracts, base_units, usd, price

Concrete spec factories (us_stock, forex, future, index_cfd, etc.)
register themselves with SpecRegistry on import — engine never names
them directly. This keeps engine.py free of `if asset_class == X`
branches.
"""

from .enum import AssetClass
from .spec import AssetSpec
from .resolver import (
    SpecRegistry, resolve, UnknownSymbol, SpecMismatchError,
)
from .currency_service import (
    CurrencyService, StaleRate, NoRateAvailable,
)
from .types import (
    Currency, DEFAULT_BASE_CURRENCY,
    Quantity, QuantityUnit, QuantityUnitMismatch,
    Money, CurrencyMismatch,
    Price,
    shares, contracts, base_units, cfd_units, usd, money, price,
)

# Concrete spec modules — imported for the side-effect of registering
# their resolvers with SpecRegistry. Order matters when a symbol could
# match multiple resolvers (e.g. "ES" is both equity-shape and futures-
# shape); modules listed first register first, so FuturesSpec (when it
# lands in D3) should be imported BEFORE us_stock to win the "ES" case.
from . import us_stock  # noqa: F401 — registers US_EQUITY resolver
from . import forex     # noqa: F401 — registers FX_CASH resolver
from . import cfds      # noqa: F401 — registers INDEX_CFD / SHARE_CFD / FX_CFD resolvers
from . import future    # noqa: F401 — registers FUTURE resolver

__all__ = [
    # Top-level
    "AssetClass", "AssetSpec",
    "SpecRegistry", "resolve", "UnknownSymbol", "SpecMismatchError",
    "CurrencyService", "StaleRate", "NoRateAvailable",
    # Types
    "Currency", "DEFAULT_BASE_CURRENCY",
    "Quantity", "QuantityUnit", "QuantityUnitMismatch",
    "Money", "CurrencyMismatch",
    "Price",
    # Constructors
    "shares", "contracts", "base_units", "cfd_units",
    "usd", "money", "price",
]
